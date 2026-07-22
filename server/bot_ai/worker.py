"""Isolated strategic bot worker.

Target searches, visibility tests, behavior selection, and path queries run in
this process.  The server receives only expiring intentions; all authoritative
movement and actions remain in the gameplay process.
"""

from __future__ import annotations

import heapq
import logging
import math
import queue
import random
import time
from dataclasses import dataclass, field
from typing import Iterable

import shared.constants as C

from .messages import (
    BotAction,
    BotActionKind,
    BotIntent,
    BotIntentPriority,
    BotProfile,
    LookIntent,
    MapSnapshot,
    MovementAffordance,
    MovementIntent,
    PerceptionFrame,
    PlayerSnapshot,
    Vector3,
    WorkerHeartbeat,
    WorkerShutdown,
    WorldDelta,
    map_snapshot_vxl_bytes,
)
from .combat_profiles import envelope_for
from .prefab_policy import bot_prefab_block_count, bot_prefab_is_suitable
from .policies import ModeBotDecision, _formation_point, objective_decision_for
from .snapshot_transport import MapSnapshotAssembler, SnapshotTransportError
from .voxel_navigation import VoxelActionPlanner, VoxelTerrain, WATERBED_SUPPORT_Z
from server.projectiles import PROJECTILE_SPECS

# Extracting a layered 32x32 Recast tile performs roughly 300k voxel probes.
# A cross-map objective may span sixteen tiles and one worker batch may carry
# twelve bots; warming every corridor synchronously starves all intentions for
# several seconds.  Build one tile per coalesced batch and use bounded voxel
# A* until the native corridor is ready.
_NATIVE_TILE_BUILDS_PER_BATCH = 1

# Position-holding/assault roles whose goals may shift onto commanding
# terrain; carriers, hunters, and escorts always keep their exact goals.
_TACTICAL_REFINE_ROLES = frozenset(
    {
        "team_assault_enemy_side",
        "ctf_defend",
        "classic_ctf_defend",
        "vip_guard_formation",
    }
)

try:
    import py_trees
except ImportError:  # pragma: no cover - dependency failure is operational
    py_trees = None


logger = logging.getLogger(__name__)

_VISUAL_RANGE = 160.0
_UNALERTED_FOV_COS = math.cos(math.radians(120.0 / 2.0))
_ALERTED_FOV_COS = math.cos(math.radians(180.0 / 2.0))
_CONTACT_LIFETIME = 5.0
_MAX_PATH_EXPANSIONS = 2048
_MAX_PATH_RADIUS = 64
_STUCK_TRIGGER_SECONDS = 0.6
_STUCK_RETRY_SECONDS = 0.6
_ROUTE_ESCAPE_STALL_SECONDS = 1.0
_REGIONAL_PROGRESS_DISTANCE = 4.0
_REGIONAL_STALL_SECONDS = 6.0
# A decision sampled at 10 Hz can legitimately have one 200 ms gap while an
# 8 Hz schedule catches up. Keep enough lease margin for process/queue jitter
# without allowing stale suggestions to survive mode, map, life, or topology
# validation in the director.
_INTENT_TTL_SECONDS = 0.4
_TEAM_ORIENTED_SPACING_SECONDS = 1.25
_DEPLOYABLE_TOOLS = frozenset(
    int(tool)
    for tool in (
        C.DYNAMITE_TOOL,
        C.LANDMINE_TOOL,
        C.C4_TOOL,
        C.RADAR_STATION_TOOL,
        C.MEDPACK_TOOL,
        C.MG_TOOL,
        C.ROCKET_TURRET_TOOL,
        C.DISGUISE_TOOL,
    )
)
_ORIENTED_TOOLS = frozenset(int(tool) for tool in PROJECTILE_SPECS) - {
    int(C.DYNAMITE_TOOL),
    int(C.LANDMINE_TOOL),
    int(C.C4_TOOL),
}
_GENERIC_DEPLOYABLE_EXCLUSIONS = frozenset(
    {int(C.DYNAMITE_TOOL), int(C.C4_TOOL)}
)
_ZOMBIE_CLASSES = frozenset({
    int(C.CLASS_ZOMBIE),
    int(C.CLASS_FAST_ZOMBIE),
    int(C.CLASS_JUMP_ZOMBIE),
})
_SNIPER_TOOLS = frozenset(
    {int(C.SNIPER_TOOL), int(C.SNIPER2_TOOL)}
)
_TRAVERSAL_PREFAB_TOKENS = (
    "bridge",
    "platform",
    "corridor",
    "tube",
)
_CLIMB_PREFAB_TOKENS = (
    "ladder",
    "steps",
    "stair",
    "platform",
    "tower",
)
_COVER_PREFAB_TOKENS = (
    "barricade",
    "wall",
    "barrier",
    "bunker",
    "shield",
    "caltrop",
    "dome",
    "tower",
    "crowsnest",
    "plug",
)


def _distance_squared(a: Vector3, b: Vector3) -> float:
    return sum((a[index] - b[index]) ** 2 for index in range(3))


def _normalized_xy(dx: float, dy: float) -> Vector3:
    length = math.hypot(dx, dy)
    if length <= 1e-6:
        return 0.0, 0.0, 0.0
    return dx / length, dy / length, 0.0


@dataclass(slots=True)
class LastSeenContact:
    """Frozen hidden-enemy knowledge with growing uncertainty."""

    player_id: int
    generation: int
    position: Vector3
    velocity: Vector3
    seen_at: float
    uncertainty: float = 0.0

    def age(self, now: float) -> float:
        """Return non-negative seconds since the last visible sample."""

        return max(0.0, now - self.seen_at)


@dataclass(frozen=True, slots=True)
class _TeamReport:
    """Delayed frozen sighting shared with teammates, never live tracking."""

    team: int
    reporter_id: int
    target_id: int
    target_generation: int
    position: Vector3
    velocity: Vector3
    deliver_at: float
    expires_at: float


@dataclass(slots=True)
class _BrainState:
    mode_epoch: int = -1
    life_id: int = -1
    contacts: dict[int, LastSeenContact] = field(default_factory=dict)
    target_id: int | None = None
    acquired_at: float = 0.0
    path: list[Vector3] = field(default_factory=list)
    path_goal: Vector3 | None = None
    path_topology_version: int = -1
    next_path_request_at: float = 0.0
    patrol_heading: float = 0.0
    next_patrol_turn: float = 0.0
    next_decision_at: float = 0.0
    next_world_action_at: float = 0.0
    next_traversal_prefab_at: float = 0.0
    last_world_action: dict[int, float] = field(default_factory=dict)
    last_path_direction: Vector3 = (0.0, 0.0, 0.0)
    last_position: Vector3 | None = None
    last_progress_at: float = 0.0
    # Recovery pacing is deliberately separate from real displacement.
    # Writing recovery attempts into ``last_progress_at`` used to let a bot
    # push against the same base wall forever while appearing healthy.
    next_stuck_recovery_at: float = 0.0
    stuck_attempts: int = 0
    # Long-horizon strategic progress catches short oscillations that produce
    # valid per-frame displacement without ever leaving a base bottleneck.
    strategic_progress_at: float = 0.0
    strategic_goal_distance: float | None = None
    regional_progress_anchor: Vector3 | None = None
    regional_progress_at: float = 0.0
    aim_head: bool = False
    delayed_target_velocity: Vector3 = (0.0, 0.0, 0.0)
    next_oriented_at: float = 0.0
    last_affordance: MovementAffordance = MovementAffordance.WALK
    next_combat_jump_at: float = 0.0
    next_strafe_switch_at: float = 0.0
    strafe_sign: float = 1.0
    next_cover_build_at: float = 0.0
    next_breach_at: float = 0.0
    next_zombie_build_at: float = 0.0
    # Stationary-weapon hold/reposition rhythm (snipers, machine guns).
    next_reposition_at: float = 0.0
    reposition_until: float = 0.0
    # Class-behavior cooldowns and own-explosive avoidance.
    next_medic_support_at: float = 0.0
    next_dynamite_at: float = 0.0
    retreat_from: Vector3 | None = None
    retreat_until: float = 0.0
    next_fortify_build_at: float = 0.0
    tactical_goal: Vector3 | None = None
    tactical_goal_until: float = 0.0
    # Humanization: per-acquisition hesitation and cover peek rhythm.
    reaction_bonus: float = 0.0
    peek_until: float = 0.0
    hold_until: float = 0.0
    last_action_feedback_frame: int = -1
    # Two-phase self-build escape: jump first so the authoritative body-cell
    # validator permits the block below, then place it while airborne.
    escape_build_cell: tuple[int, int, int] | None = None
    escape_build_until: float = 0.0
    escape_direction: Vector3 = (0.0, 0.0, 0.0)
    # After bounded dig/jump/build attempts fail, leave the failed Detour
    # corridor briefly through a safe local voxel route before asking for the
    # strategic objective again. This prevents immediate selection of the
    # identical blocked corridor.
    route_escape_goal: Vector3 | None = None
    route_escape_until: float = 0.0
    route_escape_started_at: float = 0.0
    route_escape_failures: int = 0
    # Resource pursuit is bounded: malformed/embedded official-map pickups
    # must not capture a bot forever. Position is part of the key so a crate
    # that falls with terrain can be considered again at its reachable spot.
    resource_target: tuple[int, Vector3] | None = None
    resource_target_since: float = 0.0
    ignored_resources: set[tuple[int, Vector3]] = field(default_factory=set)


class _DecisionComposer:
    """Small inspectable behavior tree selecting the TDM decision branch."""

    def __init__(self) -> None:
        self.visible = False
        self.contact = False
        self.objective = False
        self.stimulus = False
        self.result = "patrol"
        self.tree = None
        if py_trees is None:
            return

        composer = self

        class _Condition(py_trees.behaviour.Behaviour):
            def __init__(self, name: str, attribute: str) -> None:
                super().__init__(name=name)
                self.attribute = attribute

            def update(self):
                return (
                    py_trees.common.Status.SUCCESS
                    if bool(getattr(composer, self.attribute))
                    else py_trees.common.Status.FAILURE
                )

        class _Select(py_trees.behaviour.Behaviour):
            def __init__(self, name: str, value: str) -> None:
                super().__init__(name=name)
                self.value = value

            def update(self):
                composer.result = self.value
                return py_trees.common.Status.SUCCESS

        root = py_trees.composites.Selector(name="TDM Utility", memory=False)
        root.add_children(
            [
                py_trees.composites.Sequence(
                    name="Engage Visible Enemy",
                    memory=False,
                    children=[
                        _Condition("Has Fresh Visibility", "visible"),
                        _Select("Select Engage", "engage"),
                    ],
                ),
                py_trees.composites.Sequence(
                    name="Pursue Mode Objective",
                    memory=False,
                    children=[
                        _Condition("Has Mode Objective", "objective"),
                        _Select("Select Objective", "objective"),
                    ],
                ),
                py_trees.composites.Sequence(
                    name="Investigate Last Seen",
                    memory=False,
                    children=[
                        _Condition("Has Last Seen Contact", "contact"),
                        _Select("Select Investigate", "investigate"),
                    ],
                ),
                py_trees.composites.Sequence(
                    name="Investigate Sound",
                    memory=False,
                    children=[
                        _Condition("Has Audible Stimulus", "stimulus"),
                        _Select("Select Sound", "sound"),
                    ],
                ),
                _Select("Select Patrol", "patrol"),
            ]
        )
        self.tree = py_trees.trees.BehaviourTree(root)

    def choose(
        self,
        *,
        visible: bool,
        objective: bool,
        contact: bool,
        stimulus: bool = False,
    ) -> str:
        """Tick one non-blocking tree and return its selected branch."""

        self.visible = bool(visible)
        self.objective = bool(objective)
        self.contact = bool(contact)
        self.stimulus = bool(stimulus)
        if self.tree is None:
            if visible:
                return "engage"
            if objective:
                return "objective"
            if contact:
                return "investigate"
            return "sound" if stimulus else "patrol"
        self.tree.tick()
        return self.result


class WorkerVoxelWorld:
    """Worker-owned VXL used for LOS and bounded layered path queries."""

    def __init__(self) -> None:
        self.map_epoch = -1
        self.topology_version = -1
        self._vxl = None
        self._native_nav = None
        self._built_tiles: set[tuple[int, int]] = set()
        self._dirty_tiles: set[tuple[int, int]] = set()
        self._native_tile_build_budget = _NATIVE_TILE_BUILDS_PER_BATCH
        self._last_affordance: dict[int, MovementAffordance] = {}
        # Resolve ``self.solid`` at call time so source-only fixtures and live
        # VXL reloads share exactly the same surface semantics.
        self._terrain = VoxelTerrain(lambda x, y, z: self.solid(x, y, z))
        self.action_planner = VoxelActionPlanner(self._terrain)
        from .tactical_map import TacticalMap

        self.tactical = TacticalMap()

    @property
    def ready(self) -> bool:
        return self._vxl is not None

    def begin_batch(self) -> None:
        """Renew bounded native-navigation work for one worker input batch."""

        self._native_tile_build_budget = _NATIVE_TILE_BUILDS_PER_BATCH

    def load(self, snapshot: MapSnapshot) -> None:
        """Build a private native VXL from immutable snapshot bytes."""

        self.map_epoch = int(snapshot.map_epoch)
        self.topology_version = int(snapshot.topology_version)
        self.action_planner.invalidate_water_routes()
        self._vxl = None
        self._native_nav = None
        self._built_tiles.clear()
        self._dirty_tiles.clear()
        self._native_tile_build_budget = _NATIVE_TILE_BUILDS_PER_BATCH
        self._last_affordance.clear()
        try:
            raw_vxl = map_snapshot_vxl_bytes(snapshot)
        except ValueError:
            # A corrupt or truncated process message must not leave an old
            # collision map attached to tactical perception. The supervisor
            # watchdog will replace this child if it cannot answer live frames.
            logger.exception("AI worker could not decode navigation VXL")
            self.tactical.attach(None)
            return
        if not raw_vxl:
            self.tactical.attach(None)
            return
        try:
            from server.bot_ai.compact_vxl import CompactVoxelMap

            self._vxl = CompactVoxelMap(raw_vxl)
            # The bridge retains a canonical overlay for worker restarts and
            # overflow rebases. Applying it while loading yields one coherent
            # current navigation snapshot without serializing VXL on the game
            # thread.
            for change in snapshot.changed_cells:
                if change.solid:
                    self._vxl.set_point(
                        int(change.x),
                        int(change.y),
                        int(change.z),
                        0x80000000 | (int(change.color) & 0xFFFFFF),
                    )
                else:
                    self._vxl.remove_point_nochecks(
                        int(change.x), int(change.y), int(change.z)
                    )
            try:
                from server.bot_ai.recast import RecastNavigator

                native = RecastNavigator()
                self._native_nav = native if native.ready else None
            except (ImportError, OSError, RuntimeError):
                # Source-only deployments retain the bounded layered A* below.
                logger.warning("Native Recast extension unavailable; using fallback")
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            # Fail closed: a worker with no collision world may patrol, but it
            # must not claim visual contact or request a shot.
            logger.exception("AI worker could not load navigation VXL")
            self._vxl = None
        self.tactical.attach(self._vxl)

    def apply(self, delta: WorldDelta) -> None:
        """Apply one monotonically nondecreasing canonical terrain batch."""

        if int(delta.map_epoch) != self.map_epoch:
            return
        # One canonical topology commit may be split into several bounded
        # pipe records. Reapplying an equal version is safe because voxel
        # writes/removals are idempotent; only genuinely older deltas fail.
        if int(delta.topology_version) < self.topology_version:
            return
        # A bridge can create a shore and a collapse can remove the next
        # escape cell. Invalidate only routes whose columns were touched so a
        # remote bullet hole cannot repeat a full-sea search for every swimmer.
        changed_columns = frozenset(
            (int(change.x), int(change.y)) for change in delta.changed_cells
        )
        self.action_planner.invalidate_water_routes(changed_columns)
        if self._vxl is None:
            self.topology_version = int(delta.topology_version)
            return
        for change in delta.changed_cells:
            if change.solid:
                self._vxl.set_point(
                    int(change.x),
                    int(change.y),
                    int(change.z),
                    0x80000000 | (int(change.color) & 0xFFFFFF),
                )
            else:
                self._vxl.remove_point_nochecks(
                    int(change.x), int(change.y), int(change.z)
                )
            tile_x, tile_y = int(change.x) // 32, int(change.y) // 32
            for offset_x in (-1, 0, 1):
                for offset_y in (-1, 0, 1):
                    neighbor = tile_x + offset_x, tile_y + offset_y
                    if 0 <= neighbor[0] < 16 and 0 <= neighbor[1] < 16:
                        self._dirty_tiles.add(neighbor)
            self.tactical.mark_dirty(int(change.x), int(change.y))
        self.topology_version = int(delta.topology_version)

    def solid(self, x: int, y: int, z: int) -> bool:
        """Return collision occupancy, treating invalid worker state as solid."""

        if self._vxl is None:
            return True
        if not (0 <= x < 512 and 0 <= y < 512 and 0 <= z < 240):
            return True
        try:
            return bool(self._vxl.get_solid(x, y, z))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return True

    def has_line_of_sight(self, origin: Vector3, target: Vector3) -> bool:
        """DDA eye ray that reveals nothing when collision queries fail."""

        if self._vxl is None:
            return False
        dx = target[0] - origin[0]
        dy = target[1] - origin[1]
        dz = target[2] - origin[2]
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        if distance <= 1e-6:
            return True
        # Half-block sampling is sufficient for a voxel field and remains
        # bounded to 320 probes at the maximum visual consideration distance.
        steps = max(1, min(320, int(math.ceil(distance * 2.0))))
        for index in range(1, steps):
            fraction = index / steps
            x = int(math.floor(origin[0] + dx * fraction))
            y = int(math.floor(origin[1] + dy * fraction))
            z = int(math.floor(origin[2] + dz * fraction))
            if self.solid(x, y, z):
                return False
        return True

    def blocking_cell(
        self, position: Vector3, direction: Vector3
    ) -> tuple[int, int, int] | None:
        """Return the immediate body-height obstruction in ``direction``."""

        dx, dy, _ = direction
        if math.hypot(dx, dy) <= 1e-6:
            return None
        x = int(math.floor(position[0] + dx * 0.9))
        y = int(math.floor(position[1] + dy * 0.9))
        support_z = int(round(position[2] + 2.25))
        for z in (support_z - 2, support_z - 1):
            if self.solid(x, y, z):
                return x, y, z
        return None

    def bridge_cell(
        self, position: Vector3, direction: Vector3
    ) -> tuple[int, int, int] | None:
        """Return one face-supported missing floor cell directly ahead."""

        dx, dy, _ = direction
        if math.hypot(dx, dy) <= 1e-6:
            return None
        current_x = int(math.floor(position[0]))
        current_y = int(math.floor(position[1]))
        x = int(math.floor(position[0] + dx * 1.05))
        y = int(math.floor(position[1] + dy * 1.05))
        support_z = int(round(position[2] + 2.25))
        if not self.solid(current_x, current_y, support_z):
            return None
        if self.solid(x, y, support_z):
            return None
        if self.solid(x, y, support_z - 1) or self.solid(x, y, support_z - 2):
            return None
        return x, y, support_z

    def water_bridge_line(
        self,
        position: Vector3,
        direction: Vector3,
        *,
        max_cells: int = 6,
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
        """Return a face-connected floor line across an immediate open gap.

        Dry navigation never treats the stock waterbed as walkable.  When a
        route reaches the shore (or another deep gap), this bounded probe lets
        a builder extend the current floor with one native ``BlockLine``.  The
        gameplay thread still validates stock, protected zones, bodies, and
        current terrain before committing it.
        """

        dx, dy, _ = direction
        if math.hypot(dx, dy) <= 1e-6:
            return None
        step_x, step_y = (
            (1 if dx > 0.0 else -1, 0)
            if abs(dx) >= abs(dy)
            else (0, 1 if dy > 0.0 else -1)
        )
        start_x = int(math.floor(position[0]))
        start_y = int(math.floor(position[1]))
        support_z = int(round(position[2] + 2.25))
        if not self.solid(start_x, start_y, support_z):
            return None
        cells: list[tuple[int, int, int]] = []
        for distance in range(1, max(1, min(8, int(max_cells))) + 1):
            x = start_x + step_x * distance
            y = start_y + step_y * distance
            if not (0 <= x < 512 and 0 <= y < 512):
                break
            if self.solid(x, y, support_z):
                # Reached the far bank.  A single-cell hole is handled by the
                # ordinary bridge action; reserve BlockLine for real spans.
                break
            if self.solid(x, y, support_z - 1) or self.solid(
                x, y, support_z - 2
            ):
                break
            cells.append((x, y, support_z))
        if len(cells) < 2:
            return None
        return cells[0], cells[-1]

    def narrow_bridge_shoulder_line(
        self,
        position: Vector3,
        direction: Vector3,
        *,
        max_cells: int = 6,
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
        """Return the missing lane beside a supported one-cell bridge.

        A native body can stand on a single voxel lane only while perfectly
        centered. Real movement reaches it with sub-cell drift, so the live
        body-width probe correctly refuses to continue. Extend the shoulder on
        the side the body currently overhangs; every proposed cell remains
        face-supported by the existing center lane.
        """

        dx, dy, _ = direction
        if math.hypot(dx, dy) <= 1e-6:
            return None
        step_x, step_y = (
            (1 if dx > 0.0 else -1, 0)
            if abs(dx) >= abs(dy)
            else (0, 1 if dy > 0.0 else -1)
        )
        side_x, side_y = -step_y, step_x
        start_x = int(math.floor(position[0]))
        start_y = int(math.floor(position[1]))
        support_z = int(round(position[2] + 2.25))
        if not self.solid(start_x, start_y, support_z):
            return None
        cross_track = (
            (float(position[0]) - (float(start_x) + 0.5)) * side_x
            + (float(position[1]) - (float(start_y) + 0.5)) * side_y
        )
        preferred_side = 1 if cross_track >= 0.0 else -1
        limit = max(1, min(8, int(max_cells)))
        for side_sign in (preferred_side, -preferred_side):
            cells: list[tuple[int, int, int]] = []
            for distance in range(0, limit + 1):
                center_x = start_x + step_x * distance
                center_y = start_y + step_y * distance
                if not self.solid(center_x, center_y, support_z):
                    break
                x = center_x + side_x * side_sign
                y = center_y + side_y * side_sign
                if not (0 <= x < 512 and 0 <= y < 512):
                    break
                if self.solid(x, y, support_z):
                    if cells:
                        break
                    continue
                if self.solid(x, y, support_z - 1) or self.solid(
                    x, y, support_z - 2
                ):
                    if cells:
                        break
                    continue
                cells.append((x, y, support_z))
                if len(cells) >= limit:
                    break
            if cells:
                return cells[0], cells[-1]
        return None

    def overhead_block(self, position: Vector3) -> tuple[int, int, int] | None:
        """Return the closest solid cell trapping the bot above its head."""

        x = int(math.floor(position[0]))
        y = int(math.floor(position[1]))
        support_z = int(round(position[2] + 2.25))
        for z in range(support_z - 3, max(-1, support_z - 7), -1):
            if self.solid(x, y, z):
                return x, y, z
        return None

    def emergency_drop(
        self,
        position: Vector3,
        *,
        max_drop: int = 32,
    ) -> tuple[Vector3, Vector3] | None:
        """Find an adjacent clear ledge exit after all safe routes fail.

        Ordinary navigation permits drops of at most four voxels. A collapse
        can nevertheless isolate a non-builder on a tall one-cell pillar. The
        worker may choose this edge only after repeated recovery failures; the
        gameplay process still applies normal gravity and fall damage.
        """

        current = self._terrain.classify(
            int(math.floor(position[0])),
            int(math.floor(position[1])),
            position[2],
            vertical_span=5,
        )
        if current is None:
            return None
        candidates: list[tuple[float, Vector3, Vector3]] = []
        for dx, dy in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),
        ):
            x, y = current.x + dx, current.y + dy
            if not (1 <= x < 511 and 1 <= y < 511):
                continue
            # The body must be able to cross the ledge at its current height.
            if self.solid(x, y, current.support_z - 1) or self.solid(
                x, y, current.support_z - 2
            ):
                continue
            limit = min(WATERBED_SUPPORT_Z, current.support_z + max_drop + 1)
            for support_z in range(current.support_z + 5, limit):
                if not self.solid(x, y, support_z):
                    continue
                if self.solid(x, y, support_z - 1) or self.solid(
                    x, y, support_z - 2
                ):
                    continue
                fall = float(support_z - current.support_z)
                direction = _normalized_xy(float(dx), float(dy))
                landing = (
                    float(x) + 0.5,
                    float(y) + 0.5,
                    float(support_z) - 2.25,
                )
                candidates.append((fall, direction, landing))
                break
        if not candidates:
            return None
        _fall, direction, landing = min(candidates, key=lambda row: row[0])
        return direction, landing

    def hole_escape(
        self, position: Vector3, preferred: Vector3
    ) -> tuple[Vector3, int] | None:
        """Find a nearby dry ledge above the bot and report its climb height."""

        current = self._terrain.classify(
            int(math.floor(position[0])),
            int(math.floor(position[1])),
            position[2],
            vertical_span=5,
        )
        if current is None:
            return None
        preferred_xy = _normalized_xy(preferred[0], preferred[1])
        candidates: list[tuple[float, Vector3, int]] = []
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            alignment = dx * preferred_xy[0] + dy * preferred_xy[1]
            for distance in (1, 2):
                sample = self._terrain.classify(
                    current.x + dx * distance,
                    current.y + dy * distance,
                    position[2],
                    vertical_span=16,
                )
                if sample is None or sample.water:
                    continue
                rise = current.support_z - sample.support_z
                if rise <= 0:
                    continue
                candidates.append(
                    (
                        distance + rise * 0.15 - alignment * 0.35,
                        (float(dx), float(dy), 0.0),
                        rise,
                    )
                )
        if not candidates:
            return None
        _score, direction, rise = min(candidates, key=lambda item: item[0])
        return direction, rise

    def jump_build_cell(
        self, position: Vector3
    ) -> tuple[int, int, int] | None:
        """Return the empty face-supported cell directly below a jump."""

        x = int(math.floor(position[0]))
        y = int(math.floor(position[1]))
        support_z = int(round(position[2] + 2.25))
        target = (x, y, support_z - 1)
        if (
            support_z <= 2
            or not self.solid(x, y, support_z)
            or self.solid(*target)
        ):
            return None
        return target

    def cover_direction(
        self, position: Vector3, threat: Vector3
    ) -> Vector3:
        """Choose a nearby standable point occluded from ``threat``.

        This bounded eight-direction probe runs only in the worker. It never
        exposes the selected hidden position as target knowledge and performs
        no server-thread raycasts.
        """

        candidates: list[tuple[float, Vector3]] = []
        for radius in (3.0, 5.0):
            for index in range(8):
                angle = index * (math.pi / 4.0)
                x = int(math.floor(position[0] + math.cos(angle) * radius))
                y = int(math.floor(position[1] + math.sin(angle) * radius))
                node = self._standing_node(x, y, position[2], vertical_span=2)
                if node is None:
                    continue
                cover = (
                    float(node[0]) + 0.5,
                    float(node[1]) + 0.5,
                    float(node[2]) - 2.25,
                )
                cover_eye = cover[0], cover[1], cover[2]
                if self.has_line_of_sight(cover_eye, threat):
                    continue
                travel = math.hypot(cover[0] - position[0], cover[1] - position[1])
                separation = math.hypot(cover[0] - threat[0], cover[1] - threat[1])
                candidates.append((travel - separation * 0.05, cover))
        if not candidates:
            return 0.0, 0.0, 0.0
        _score, best = min(candidates, key=lambda item: item[0])
        return _normalized_xy(best[0] - position[0], best[1] - position[1])

    def cover_build_cell(
        self, position: Vector3, threat: Vector3
    ) -> tuple[int, int, int] | None:
        """Return one supported empty wall cell between bot and threat.

        The probe is deliberately local and bounded. Construction safety and
        authoritative VXL checks run again on the gameplay thread, so this is
        only a tactical suggestion and can never place through a player,
        objective zone, or teammate corridor.
        """

        direction = _normalized_xy(
            threat[0] - position[0], threat[1] - position[1]
        )
        if math.hypot(direction[0], direction[1]) <= 1e-6:
            return None
        support_z = int(round(position[2] + 2.25))
        for distance in (1.35, 1.85):
            x = int(math.floor(position[0] + direction[0] * distance))
            y = int(math.floor(position[1] + direction[1] * distance))
            lower = (x, y, support_z - 1)
            upper = (x, y, support_z - 2)
            if not self.solid(*lower) and self.solid(x, y, support_z):
                return lower
            if self.solid(*lower) and not self.solid(*upper):
                return upper
        return None

    def cover_build_line(
        self, position: Vector3, threat: Vector3
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
        """Return a supported 3--5 block wall perpendicular to ``threat``.

        A cover suggestion is deliberately a straight native ``BlockLine``:
        retail observers then see one coherent construction action instead of
        a bot spraying unrelated single-cell packets.  Every proposed cell is
        empty and supported; the gameplay-thread construction service still
        revalidates the exact footprint against live terrain, bodies, spawn
        zones, objectives, and friendly reservations.
        """

        direction = _normalized_xy(
            threat[0] - position[0], threat[1] - position[1]
        )
        if math.hypot(direction[0], direction[1]) <= 1e-6:
            return None
        support_z = int(round(position[2] + 2.25))
        # Face the broad side of an axis-aligned wall toward the threat.  AoS
        # BlockLine rasterization is deterministic for these exact endpoints.
        wall_along_y = abs(direction[0]) >= abs(direction[1])
        for distance in (1.85, 1.35):
            center_x = int(math.floor(position[0] + direction[0] * distance))
            center_y = int(math.floor(position[1] + direction[1] * distance))
            for z in (support_z - 1, support_z - 2):
                for length in (5, 3):
                    half = length // 2
                    if wall_along_y:
                        cells = tuple(
                            (center_x, center_y + offset, z)
                            for offset in range(-half, half + 1)
                        )
                    else:
                        cells = tuple(
                            (center_x + offset, center_y, z)
                            for offset in range(-half, half + 1)
                        )
                    if all(
                        not self.solid(*cell)
                        and self.solid(cell[0], cell[1], cell[2] + 1)
                        for cell in cells
                    ):
                        return cells[0], cells[-1]
        return None

    def next_path_direction(
        self,
        start: Vector3,
        goal: Vector3,
        *,
        agent_id: int = -1,
        velocity: Vector3 = (0.0, 0.0, 0.0),
        abilities: frozenset[MovementAffordance] = frozenset(),
    ) -> Vector3:
        """Return a bounded layered-grid step toward ``goal``.

        Recast/Detour replaces this compatibility navigator when the native
        extension is available.  The fallback still keeps A* outside the game
        thread and respects live voxel occupancy, caves, and local elevation.
        """

        if self._vxl is None:
            return 0.0, 0.0, 0.0
        native_direction = self._native_path_direction(
            start, goal, agent_id=agent_id, velocity=velocity
        )
        if math.hypot(native_direction[0], native_direction[1]) > 1e-6:
            native_affordance = self._immediate_affordance(
                start, native_direction, abilities
            )
            if self._direction_is_traversable(
                start, native_direction, native_affordance
            ):
                self._last_affordance[agent_id] = native_affordance
                return native_direction
        path, affordance = self._a_star(start, goal, abilities=abilities)
        if not path:
            self._last_affordance[agent_id] = MovementAffordance.WALK
            return 0.0, 0.0, 0.0
        self._last_affordance[agent_id] = affordance
        waypoint = path[min(1, len(path) - 1)]
        return _normalized_xy(waypoint[0] - start[0], waypoint[1] - start[1])

    def last_affordance(self, agent_id: int) -> MovementAffordance:
        """Return the immediate topology edge selected for one crowd agent."""

        return self._last_affordance.get(int(agent_id), MovementAffordance.WALK)

    def reset_agent_navigation(self, agent_id: int) -> None:
        """Discard one failed worker-side crowd corridor.

        Called only by the worker's bounded stuck recovery. The authoritative
        player remains untouched; the next path query creates a fresh Detour
        proxy re-anchored at the latest server position.
        """

        normalized = int(agent_id)
        self._last_affordance.pop(normalized, None)
        if self._native_nav is None:
            return
        try:
            self._native_nav.remove_crowd_agent(normalized)
        except (RuntimeError, TypeError, ValueError):
            logger.exception(
                "Native Recast crowd reset failed for bot %s", normalized
            )

    def _immediate_affordance(
        self,
        start: Vector3,
        direction: Vector3,
        abilities: frozenset[MovementAffordance],
    ) -> MovementAffordance:
        """Classify the first Recast steering edge using live voxel heights.

        Detour returns a direction, not the off-mesh action needed by native
        player physics.  Without this bridge a valid two-block climb is
        flattened to WALK and bots repeatedly run into its face.
        """

        current = self._terrain.classify(
            int(math.floor(start[0])),
            int(math.floor(start[1])),
            start[2],
            vertical_span=5,
        )
        if current is None:
            return MovementAffordance.WALK
        tested: set[tuple[int, int]] = set()
        for distance in (1.05, 2.05):
            x = int(math.floor(start[0] + direction[0] * distance))
            y = int(math.floor(start[1] + direction[1] * distance))
            if (x, y) in tested or (x, y) == (current.x, current.y):
                continue
            tested.add((x, y))
            sample = self._terrain.classify(
                x, y, start[2], vertical_span=8, clearance=2
            )
            if sample is None:
                continue
            delta_z = sample.support_z - current.support_z
            if (
                MovementAffordance.JUMP in abilities
                and (
                    -2 <= delta_z < -1
                    or (distance > 1.5 and abs(delta_z) <= 2)
                )
            ):
                return MovementAffordance.JUMP
            if delta_z > 1 and MovementAffordance.DROP in abilities:
                return MovementAffordance.DROP
            return MovementAffordance.WALK
        return MovementAffordance.WALK

    def _direction_is_traversable(
        self,
        start: Vector3,
        direction: Vector3,
        affordance: MovementAffordance,
    ) -> bool:
        """Mirror the gameplay motor's body-width live movement probe."""

        return self._terrain.direction_is_traversable(
            start,
            direction,
            affordance,
        )

    @property
    def native_tile_count(self) -> int:
        """Return built Recast tiles for diagnostics and smoke fixtures."""

        if self._native_nav is None:
            return 0
        return int(self._native_nav.tile_count)

    def _native_path_direction(
        self,
        start: Vector3,
        goal: Vector3,
        *,
        agent_id: int,
        velocity: Vector3,
    ) -> Vector3:
        if self._native_nav is None:
            return 0.0, 0.0, 0.0
        tiles = self._tile_corridor(start, goal)
        for tile in tiles:
            if tile not in self._built_tiles or tile in self._dirty_tiles:
                if self._native_tile_build_budget <= 0:
                    # The voxel A* fallback remains immediately available;
                    # later batches incrementally warm the rest of Recast.
                    return 0.0, 0.0, 0.0
                self._native_tile_build_budget -= 1
                self._rebuild_native_tile(*tile)
                if tile not in self._built_tiles or tile in self._dirty_tiles:
                    return 0.0, 0.0, 0.0
        native_start = (start[0], -(start[2] + 2.25), start[1])
        native_end = (goal[0], -(goal[2] + 2.25), goal[1])
        try:
            if agent_id >= 0:
                steering = self._native_nav.crowd_steer(
                    int(agent_id),
                    native_start,
                    native_end,
                    (velocity[0], -velocity[2], velocity[1]),
                    4.5,
                    12.0,
                    0.2,
                )
                if len(steering) == 3:
                    crowd_direction = _normalized_xy(
                        float(steering[0]), float(steering[2])
                    )
                    if math.hypot(crowd_direction[0], crowd_direction[1]) > 1e-6:
                        return crowd_direction
        except (RuntimeError, TypeError, ValueError):
            logger.exception("Native Recast path query failed")
            return 0.0, 0.0, 0.0
        # Do not call Detour's synchronous find_path as a live fallback here.
        # A malformed/cyclic multi-tile corridor observed on MayanJungle kept
        # that native query inside dtNavMeshQuery indefinitely, pinning the
        # single AI worker and expiring every bot intent.  A zero crowd result
        # deliberately falls through to the bounded voxel A* in
        # next_path_direction instead.
        return 0.0, 0.0, 0.0

    @staticmethod
    def _tile_corridor(start: Vector3, goal: Vector3) -> tuple[tuple[int, int], ...]:
        start_tile = int(start[0]) // 32, int(start[1]) // 32
        goal_tile = int(goal[0]) // 32, int(goal[1]) // 32
        dx = goal_tile[0] - start_tile[0]
        dy = goal_tile[1] - start_tile[1]
        steps = max(1, abs(dx), abs(dy))
        result: list[tuple[int, int]] = []
        for index in range(steps + 1):
            fraction = index / steps
            tile = (
                max(0, min(15, int(round(start_tile[0] + dx * fraction)))),
                max(0, min(15, int(round(start_tile[1] + dy * fraction)))),
            )
            if tile not in result:
                result.append(tile)
        return tuple(result)

    def _rebuild_native_tile(self, tile_x: int, tile_y: int) -> None:
        """Extract exposed layered voxel floors and rebuild one 32x32 tile."""

        if self._native_nav is None:
            return
        vertices: list[float] = []
        triangles: list[int] = []
        border = 2
        x0 = max(0, tile_x * 32 - border)
        x1 = min(512, (tile_x + 1) * 32 + border)
        y0 = max(0, tile_y * 32 - border)
        y1 = min(512, (tile_y + 1) * 32 + border)
        for x in range(x0, x1):
            for y in range(y0, y1):
                air_run = 2
                for z in range(2, 240):
                    occupied = self.solid(x, y, z)
                    if not occupied:
                        air_run += 1
                        continue
                    # The stock map guarantees a solid waterbed at z=239.
                    # It is collision support for swimming, not dry terrain,
                    # and must never become an ordinary Recast polygon.
                    if z >= WATERBED_SUPPORT_Z:
                        air_run = 0
                        continue
                    if air_run >= 2:
                        base = len(vertices) // 3
                        height = -float(z)
                        # AoS (x,y,z-down) -> Recast (x,-z,y). Winding points
                        # toward +Y, the native library's walkable-up axis.
                        vertices.extend(
                            (
                                float(x), height, float(y),
                                float(x + 1), height, float(y),
                                float(x + 1), height, float(y + 1),
                                float(x), height, float(y + 1),
                            )
                        )
                        triangles.extend(
                            (base, base + 2, base + 1, base, base + 3, base + 2)
                        )
                    air_run = 0
        try:
            if vertices:
                built = self._native_nav.build_tile(
                    tile_x,
                    tile_y,
                    vertices,
                    triangles,
                    (float(tile_x * 32), -240.0, float(tile_y * 32)),
                    (float((tile_x + 1) * 32), 0.0, float((tile_y + 1) * 32)),
                )
            else:
                built = self._native_nav.remove_tile(tile_x, tile_y)
        except (RuntimeError, TypeError, ValueError):
            logger.exception("Native Recast tile build failed at (%s,%s)", tile_x, tile_y)
            built = False
        if built:
            self._built_tiles.add((tile_x, tile_y))
            self._dirty_tiles.discard((tile_x, tile_y))

    def _a_star(
        self,
        start: Vector3,
        goal: Vector3,
        *,
        abilities: frozenset[MovementAffordance],
    ) -> tuple[list[Vector3], MovementAffordance]:
        """Search bounded local topology including class-filtered ability edges."""

        start_node = self._standing_node(int(start[0]), int(start[1]), start[2])
        if start_node is None:
            return [], MovementAffordance.WALK
        goal_x = max(
            start_node[0] - _MAX_PATH_RADIUS,
            min(start_node[0] + _MAX_PATH_RADIUS, int(goal[0])),
        )
        goal_y = max(
            start_node[1] - _MAX_PATH_RADIUS,
            min(start_node[1] + _MAX_PATH_RADIUS, int(goal[1])),
        )
        frontier: list[tuple[float, int, tuple[int, int, int]]] = []
        sequence = 0
        heapq.heappush(frontier, (0.0, sequence, start_node))
        came_from: dict[tuple[int, int, int], tuple[int, int, int] | None] = {
            start_node: None
        }
        came_affordance: dict[tuple[int, int, int], MovementAffordance] = {
            start_node: MovementAffordance.WALK
        }
        cost = {start_node: 0.0}
        best = start_node
        best_distance = math.hypot(start_node[0] - goal_x, start_node[1] - goal_y)

        while frontier and len(came_from) <= _MAX_PATH_EXPANSIONS:
            _priority, _sequence, current = heapq.heappop(frontier)
            distance = math.hypot(current[0] - goal_x, current[1] - goal_y)
            if distance < best_distance:
                best, best_distance = current, distance
            if distance <= 0.25:
                best = current
                break
            for neighbor, affordance, edge_cost in self._neighbors(
                current, abilities=abilities
            ):
                new_cost = cost[current] + edge_cost
                if new_cost >= cost.get(neighbor, math.inf):
                    continue
                cost[neighbor] = new_cost
                sequence += 1
                heuristic = math.hypot(neighbor[0] - goal_x, neighbor[1] - goal_y)
                heapq.heappush(frontier, (new_cost + heuristic, sequence, neighbor))
                came_from[neighbor] = current
                came_affordance[neighbor] = affordance

        nodes: list[tuple[int, int, int]] = []
        cursor: tuple[int, int, int] | None = best
        while cursor is not None:
            nodes.append(cursor)
            cursor = came_from[cursor]
        nodes.reverse()
        path = [
            (float(x) + 0.5, float(y) + 0.5, float(support_z) - 2.25)
            for x, y, support_z in nodes
        ]
        first_affordance = (
            came_affordance.get(nodes[1], MovementAffordance.WALK)
            if len(nodes) > 1
            else MovementAffordance.WALK
        )
        return path, first_affordance

    def _standing_node(
        self,
        x: int,
        y: int,
        current_player_z: float,
        *,
        vertical_span: int = 3,
        clearance: int = 2,
    ) -> tuple[int, int, int] | None:
        return self._terrain.standing_node(
            x,
            y,
            current_player_z,
            vertical_span=vertical_span,
            clearance=clearance,
        )

    def _neighbors(
        self,
        node: tuple[int, int, int],
        *,
        abilities: frozenset[MovementAffordance],
    ) -> Iterable[tuple[tuple[int, int, int], MovementAffordance, float]]:
        """Yield walk plus bounded crouch/jump/drop/jetpack off-mesh edges."""

        x, y, support_z = node
        player_z = float(support_z) - 2.25
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < 512 and 0 <= ny < 512):
                continue
            neighbor = self._standing_node(
                nx, ny, player_z, vertical_span=8, clearance=2
            )
            if neighbor is not None:
                delta_z = neighbor[2] - support_z
                if abs(delta_z) <= 1:
                    yield neighbor, MovementAffordance.WALK, 1.0 + abs(delta_z) * 0.25
                    continue
                if (
                    delta_z < 0
                    and -delta_z <= 2
                    and MovementAffordance.JUMP in abilities
                ):
                    yield neighbor, MovementAffordance.JUMP, 1.8 + -delta_z * 0.35
                    continue
                if (
                    1 < delta_z <= 4
                    and MovementAffordance.DROP in abilities
                ):
                    yield neighbor, MovementAffordance.DROP, 1.2 + delta_z * 0.15
                    continue
                if (
                    abs(delta_z) <= 8
                    and MovementAffordance.JETPACK in abilities
                ):
                    yield neighbor, MovementAffordance.JETPACK, 3.0 + abs(delta_z) * 0.2
                    continue

            if MovementAffordance.CROUCH in abilities:
                crouched = self._standing_node(
                    nx, ny, player_z, vertical_span=1, clearance=1
                )
                if crouched is not None and abs(crouched[2] - support_z) <= 1:
                    yield crouched, MovementAffordance.CROUCH, 1.4
                    continue

            # Explicit off-mesh gaps are deliberately local. Arbitrary voxel
            # combinations belong to the breach/build affordance layer.
            max_distance = 4 if MovementAffordance.JETPACK in abilities else 2
            for distance in range(2, max_distance + 1):
                tx, ty = x + dx * distance, y + dy * distance
                if not (0 <= tx < 512 and 0 <= ty < 512):
                    break
                landing = self._standing_node(
                    tx, ty, player_z, vertical_span=8, clearance=2
                )
                if landing is None:
                    continue
                delta_z = landing[2] - support_z
                if (
                    distance == 2
                    and abs(delta_z) <= 2
                    and MovementAffordance.JUMP in abilities
                ):
                    yield landing, MovementAffordance.JUMP, 2.6 + abs(delta_z) * 0.3
                    break
                if (
                    abs(delta_z) <= 8
                    and MovementAffordance.JETPACK in abilities
                ):
                    yield landing, MovementAffordance.JETPACK, 3.5 + distance * 0.5
                    break


class BotBrain:
    """Fair perception memory and deterministic TDM utility decisions."""

    def __init__(
        self,
        world: WorkerVoxelWorld,
        seed: int = 0,
        *,
        decision_hz: float = 0.0,
        path_requests_per_second: float = 24.0,
    ) -> None:
        self.world = world
        self._rng = random.Random(int(seed))
        self._states: dict[tuple[int, int], _BrainState] = {}
        self._map_epoch = -1
        # Shared fortify sites keyed (map_epoch, team) -> (site, computed_at).
        self._fortify_sites: dict[tuple[int, int], tuple[Vector3, float]] = {}
        self._decision_interval = (
            1.0 / max(1.0, float(decision_hz)) if decision_hz > 0.0 else 0.0
        )
        self._path_rate = max(1.0, float(path_requests_per_second))
        self._path_tokens = self._path_rate
        self._path_refill_at = time.monotonic()
        self._composer = _DecisionComposer()
        self._team_reports: list[_TeamReport] = []
        self._last_team_report: dict[tuple[int, int], float] = {}
        self._team_oriented_ready_at: dict[int, float] = {}

    def reset_for_map(self, map_epoch: int) -> None:
        """Discard every map-scoped cache when the loaded world changes."""

        normalized = int(map_epoch)
        if normalized == self._map_epoch:
            return
        self._map_epoch = normalized
        self._states.clear()
        self._fortify_sites.clear()
        self._team_reports.clear()
        self._last_team_report.clear()
        self._team_oriented_ready_at.clear()
        self._path_tokens = self._path_rate
        self._path_refill_at = time.monotonic()

    def _prune_retired_states(self, frame: PerceptionFrame) -> None:
        """Retire bot generations absent from the current frozen roster."""

        active = {
            (int(player.player_id), int(player.generation))
            for player in frame.players
        }
        retired = [key for key in self._states if key not in active]
        reset_navigation = getattr(self.world, "reset_agent_navigation", None)
        for key in retired:
            self._states.pop(key, None)
            if callable(reset_navigation):
                reset_navigation(key[0])
        active_ids = {int(player.player_id) for player in frame.players}
        self._last_team_report = {
            key: reported_at
            for key, reported_at in self._last_team_report.items()
            if key[0] in active_ids and key[1] in active_ids
        }

    def decide(self, frame: PerceptionFrame) -> BotIntent | None:
        """Produce one expiring intention for a valid observer frame."""

        observer = next(
            (
                player
                for player in frame.players
                if player.player_id == frame.observer_id
                and player.generation == frame.observer_generation
            ),
            None,
        )
        if observer is None or not observer.alive or not observer.spawned:
            return None
        if self._map_epoch != int(frame.map_epoch):
            self.reset_for_map(frame.map_epoch)
        self._prune_retired_states(frame)
        key = observer.player_id, observer.generation
        # State and cooldowns live on the authoritative perception timeline.
        # Worker completion time may drift under pathfinding load; feeding that
        # latency back into progress clocks delays stuck recovery and makes
        # behavior depend on CPU speed. _intent stamps its separate live lease.
        now = float(frame.created_at)
        state = self._states.get(key)
        state_scope_changed = state is not None and (
            int(state.mode_epoch) != int(frame.mode_epoch)
            or (
                int(state.life_id) >= 0
                and int(state.life_id) != int(observer.life_id)
            )
        )
        if state is None or state_scope_changed:
            if state_scope_changed:
                reset_navigation = getattr(
                    self.world, "reset_agent_navigation", None
                )
                if callable(reset_navigation):
                    reset_navigation(observer.player_id)
            state = _BrainState(
                mode_epoch=int(frame.mode_epoch),
                life_id=int(observer.life_id),
            )
            self._states[key] = state
        profile = frame.profile or _fallback_profile(observer.player_id)
        self._consume_action_feedback(observer, state, now)
        teleported = self._record_progress(
            state,
            observer.position,
            now,
            life_id=observer.life_id,
        )
        if teleported:
            reset_navigation = getattr(
                self.world, "reset_agent_navigation", None
            )
            if callable(reset_navigation):
                reset_navigation(observer.player_id)
        water_recovery = self._water_recovery(
            frame, observer, state, now
        )
        if water_recovery is not None:
            return water_recovery
        self._deliver_team_reports(observer, state, now)
        self._expire_contacts(state, now)
        visible = self._visible_enemies(observer, frame.players, state)

        target: PlayerSnapshot | None = None
        if visible:
            target = min(
                visible,
                key=lambda candidate: _distance_squared(observer.position, candidate.position),
            )
            if state.target_id != target.player_id:
                state.target_id = target.player_id
                state.acquired_at = now
                # Intentional head targeting stays below 20%; ordinary aim is
                # center mass and natural error may still create incidental
                # head hits.
                state.aim_head = self._rng.random() < min(
                    0.18, 0.04 + float(profile.skill) * 0.14
                )
                # Occasional double-take: lower-skill players sometimes need
                # an extra beat to commit to a new target.
                state.reaction_bonus = (
                    self._rng.uniform(0.15, 0.4)
                    if self._rng.random()
                    < (1.0 - float(profile.skill)) * 0.35
                    else 0.0
                )
            previous_contact = state.contacts.get(target.player_id)
            state.delayed_target_velocity = (
                previous_contact.velocity
                if previous_contact is not None
                and previous_contact.generation == target.generation
                else (0.0, 0.0, 0.0)
            )
            state.contacts[target.player_id] = LastSeenContact(
                player_id=target.player_id,
                generation=target.generation,
                position=target.position,
                velocity=target.velocity,
                seen_at=now,
            )
            self._queue_team_report(observer, target, now)

        # Cadence belongs to the authoritative perception timeline, not to the
        # time at which this process finishes pathfinding. Variable query cost
        # otherwise pushes a deadline just beyond the next frame and can halve
        # an equal-rate decision stream (for example 5 Hz becoming 2.5 Hz).
        decision_time = float(frame.created_at)
        if decision_time + 1e-9 < state.next_decision_at:
            return None
        if self._decision_interval > 0.0:
            scheduled_at = state.next_decision_at
            if (
                scheduled_at <= 0.0
                or decision_time - scheduled_at > self._decision_interval * 4.0
            ):
                # Do not burst through a long worker pause or map-load stall.
                state.next_decision_at = decision_time + self._decision_interval
            else:
                # Advance the schedule from its prior phase. Using ``now`` here
                # turns 8 Hz over 10 Hz perception into 5 Hz (every other
                # frame), which periodically expires otherwise valid movement.
                state.next_decision_at = scheduled_at + self._decision_interval

        # Own-explosive avoidance outranks everything, including a visible
        # enemy: running from your own lit dynamite is the human move.
        hazard_retreat = self._active_hazard_retreat(
            frame, observer, state, now
        )
        if hazard_retreat is not None:
            return hazard_retreat
        retreat = self._blast_retreat(frame, observer, state, now)
        if retreat is not None:
            return retreat
        if target is None:
            damage_reaction = self._damage_reaction(
                frame, observer, state, now
            )
            if damage_reaction is not None:
                return damage_reaction

        contact = self._best_contact(observer, state, now)
        mode_decision = self._objective_decision(frame, observer)
        mode_objective = (
            mode_decision.position if mode_decision is not None else None
        )
        if (
            mode_decision is not None
            and not str(mode_decision.role).startswith("zombie_hunt_")
            and _distance_squared(observer.position, mode_decision.position)
            <= float(mode_decision.arrival_radius) ** 2
        ):
            # At a role station, allow class actions (fortification, mines,
            # turrets, healing) to run instead of endlessly pathing in place.
            mode_objective = None
        fortify_site = None
        if (
            mode_decision is not None
            and str(getattr(mode_decision, "directive", "")) == "fortify"
        ):
            # A fortify directive overrides the loose formation point with a
            # scored defensible team site; near the site the bot builds.
            fortify_site = self._fortify_site(frame, observer, now)
            if fortify_site is not None:
                station = _formation_point(
                    fortify_site, observer.player_id, 3.5
                )
                # Arrival is judged in the horizontal plane with a loose
                # height tolerance: exact z convergence on slopes would keep
                # a stationed builder pathing forever.
                planar = math.hypot(
                    observer.position[0] - station[0],
                    observer.position[1] - station[1],
                )
                if planar > 2.5 or abs(observer.position[2] - station[2]) > 3.0:
                    mode_objective = station
                else:
                    mode_objective = None
        elif (
            mode_decision is not None
            and mode_objective is not None
            and str(mode_decision.role) in _TACTICAL_REFINE_ROLES
        ):
            refined = self._tactical_goal(observer, state, mode_decision, now)
            if refined is not None:
                mode_objective = refined
        # Active infected never abandon a living survivor for a generic ammo
        # or health crate. Their exact pursuit target is sanctioned mode state,
        # and their hand/prefab loadout cannot consume firearm ammunition.
        zombie_hunt = str(
            mode_decision.role if mode_decision is not None else ""
        ).startswith("zombie_hunt_")
        resource = (
            None
            if zombie_hunt or target is not None
            else self._resource_goal(frame, observer, state=state, now=now)
        )
        if observer.carried_entity_id >= 0:
            objective = mode_objective
            objective_role = mode_decision.role if mode_decision is not None else ""
            objective_sprint = bool(mode_decision.sprint) if mode_decision else False
        elif resource is not None:
            objective = resource
            objective_role = "resource"
            objective_sprint = False
        else:
            objective = mode_objective
            objective_role = mode_decision.role if mode_decision is not None else ""
            objective_sprint = bool(mode_decision.sprint) if mode_decision else False
        audible = self._best_stimulus(frame, observer, now)
        branch = self._composer.choose(
            visible=target is not None,
            objective=objective is not None,
            contact=contact is not None,
            stimulus=audible is not None,
        )
        if branch == "engage" and target is not None:
            if int(observer.class_id) in _ZOMBIE_CLASSES:
                # Visible survivors remain the urgent target, but a stalled
                # claw charge still needs topology recovery. Progress tracking
                # keeps this inert during a successful chase; after 0.6s it
                # may breach/build rather than returning a zero vector forever.
                recovery = self._stuck_recovery(
                    frame, observer, state, now
                )
                if recovery is not None:
                    return recovery
            return self._engage(frame, observer, target, state, profile, now)

        maintenance = self._maintenance_intent(frame, observer, now)
        if maintenance is not None:
            return maintenance

        recovery_active = (
            state.escape_build_cell is not None
            or state.route_escape_goal is not None
        )
        if branch in {"objective", "investigate", "sound"} or recovery_active:
            recovery = self._stuck_recovery(frame, observer, state, now)
            if recovery is not None:
                return recovery

        medic = self._medic_support_intent(frame, observer, state, now)
        if medic is not None:
            return medic

        if branch == "objective" and objective is not None:
            movement = self._path_direction(
                observer.position,
                objective,
                state,
                now,
                agent_id=observer.player_id,
                velocity=observer.velocity,
                abilities=self._movement_abilities(observer),
            )
            if math.hypot(movement[0], movement[1]) <= 0.1:
                movement = self._local_objective_route(
                    frame, observer, state, objective
                )
            breach = self._proactive_breach(
                frame,
                observer,
                state,
                now,
                movement,
            )
            if breach is None and str(objective_role).startswith(
                "zombie_hunt_"
            ):
                # Navmesh steering happily orbits a rock that hides the prey
                # forever. A real zombie digs straight through it.
                breach = self._zombie_hunt_breach(
                    frame, observer, state, objective, now
                )
            if breach is not None:
                return breach
            return self._intent(
                frame,
                now,
                movement=MovementIntent(
                    direction=movement,
                    sprint=objective_sprint,
                    affordance=state.last_affordance,
                ),
                look=LookIntent(
                    self._navigation_look(observer, movement, objective),
                    visible=False,
                ),
                debug_role=objective_role,
            )

        if branch == "investigate" and contact is not None:
            # Hidden target position is frozen at the last visible sample.
            movement = self._path_direction(
                observer.position,
                contact.position,
                state,
                now,
                agent_id=observer.player_id,
                velocity=observer.velocity,
                abilities=self._movement_abilities(observer),
            )
            demolition = self._miner_demolition(
                frame,
                observer,
                state,
                contact,
                now,
                movement,
            )
            if demolition is not None:
                return demolition
            breach = self._proactive_breach(
                frame,
                observer,
                state,
                now,
                movement,
            )
            if breach is not None:
                return breach
            return self._intent(
                frame,
                now,
                movement=MovementIntent(
                    direction=movement,
                    sprint=True,
                    affordance=state.last_affordance,
                ),
                look=LookIntent(
                    self._navigation_look(
                        observer, movement, contact.position
                    ),
                    visible=False,
                ),
            )

        if branch == "sound" and audible is not None:
            movement = self._path_direction(
                observer.position,
                audible.position,
                state,
                now,
                agent_id=observer.player_id,
                velocity=observer.velocity,
                abilities=self._movement_abilities(observer),
            )
            return self._intent(
                frame,
                now,
                movement=MovementIntent(
                    direction=movement,
                    sprint=False,
                    affordance=state.last_affordance,
                ),
                look=LookIntent(
                    self._navigation_look(
                        observer, movement, audible.position
                    ),
                    visible=False,
                ),
            )

        if fortify_site is not None and mode_objective is None:
            fortification = self._fortify_build_intent(
                frame, observer, state, fortify_site, now
            )
            if fortification is not None:
                return fortification

        world_action = self._class_world_action(
            frame,
            observer,
            state,
            profile,
            now,
            role=objective_role,
        )
        if world_action is not None:
            return world_action

        if fortify_site is not None and mode_objective is None:
            # Stationed and nothing left to build: hold the fort and scan the
            # open approaches instead of wandering off on patrol.
            return self._fortify_hold_intent(frame, observer, fortify_site, now)

        return self._patrol(frame, observer, state, now)

    def _queue_team_report(
        self,
        observer: PlayerSnapshot,
        target: PlayerSnapshot,
        now: float,
    ) -> None:
        """Schedule one 0.4-1.0s delayed sighting from a visible sample."""

        key = int(observer.player_id), int(target.player_id)
        if now - self._last_team_report.get(key, -math.inf) < 1.0:
            return
        self._last_team_report[key] = now
        delay = self._rng.uniform(0.4, 1.0)
        self._team_reports.append(
            _TeamReport(
                team=int(observer.team),
                reporter_id=int(observer.player_id),
                target_id=int(target.player_id),
                target_generation=int(target.generation),
                position=target.position,
                velocity=target.velocity,
                deliver_at=now + delay,
                expires_at=now + delay + _CONTACT_LIFETIME,
            )
        )
        if len(self._team_reports) > 256:
            del self._team_reports[: len(self._team_reports) - 256]

    def _deliver_team_reports(
        self, observer: PlayerSnapshot, state: _BrainState, now: float
    ) -> None:
        """Copy due teammate samples into frozen last-seen memory."""

        self._team_reports = [
            report for report in self._team_reports if report.expires_at > now
        ]
        for report in self._team_reports:
            if (
                report.team != int(observer.team)
                or report.reporter_id == int(observer.player_id)
                or report.deliver_at > now
            ):
                continue
            existing = state.contacts.get(report.target_id)
            if existing is not None and existing.seen_at >= report.deliver_at:
                continue
            state.contacts[report.target_id] = LastSeenContact(
                player_id=report.target_id,
                generation=report.target_generation,
                position=report.position,
                velocity=report.velocity,
                seen_at=report.deliver_at,
                uncertainty=max(1.0, (now - report.deliver_at) * 3.0),
            )

    @staticmethod
    def _best_stimulus(frame: PerceptionFrame, observer: PlayerSnapshot, now: float):
        """Choose an active approximate cue without promoting it to visibility."""

        candidates = [
            stimulus
            for stimulus in frame.stimuli
            if stimulus.expires_at > now and stimulus.source_id != observer.player_id
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda stimulus: (
                _distance_squared(observer.position, stimulus.position),
                -stimulus.created_at,
            ),
        )

    @staticmethod
    def _record_progress(
        state: _BrainState,
        position: Vector3,
        now: float,
        *,
        life_id: int | None = None,
    ) -> bool:
        """Track horizontal progress toward the requested route or goal.

        Vertical jump/water motion and sideways knockback are deliberately not
        progress; otherwise a bot can bob forever without entering recovery.
        Returns true when a respawn/teleport discontinuity invalidates all
        life-local navigation and perception memory.
        """

        previous_life = state.life_id
        life_changed = (
            life_id is not None
            and previous_life >= 0
            and int(life_id) != previous_life
        )
        if life_id is not None:
            state.life_id = int(life_id)
        previous = state.last_position
        state.last_position = position
        if previous is None:
            state.last_progress_at = now
            return False
        move_x = float(position[0]) - float(previous[0])
        move_y = float(position[1]) - float(previous[1])
        move_z = float(position[2]) - float(previous[2])
        if (
            life_changed
            or math.hypot(move_x, move_y) >= 16.0
            or abs(move_z) >= 16.0
        ):
            # Bot generation identifies the server-owned connection, not an
            # individual life. Respawn and admin teleport must not carry a
            # failed corridor/escape or last-seen target into the new body.
            state.contacts.clear()
            state.target_id = None
            state.acquired_at = 0.0
            state.path.clear()
            state.path_goal = None
            state.path_topology_version = -1
            state.next_path_request_at = 0.0
            state.last_path_direction = (0.0, 0.0, 0.0)
            state.last_affordance = MovementAffordance.WALK
            state.last_progress_at = now
            state.next_stuck_recovery_at = 0.0
            state.stuck_attempts = 0
            state.strategic_progress_at = now
            state.strategic_goal_distance = None
            state.regional_progress_anchor = None
            state.regional_progress_at = now
            state.escape_build_cell = None
            state.escape_build_until = 0.0
            state.route_escape_goal = None
            state.route_escape_until = 0.0
            state.route_escape_started_at = 0.0
            state.route_escape_failures = 0
            state.resource_target = None
            state.resource_target_since = 0.0
            return True
        route_x = float(state.last_path_direction[0])
        route_y = float(state.last_path_direction[1])
        route_length = math.hypot(route_x, route_y)
        forward_progress = 0.0
        if route_length > 1e-6:
            forward_progress = (
                move_x * route_x + move_y * route_y
            ) / route_length

        goal_progress = 0.0
        if state.path_goal is not None:
            previous_distance = math.hypot(
                float(state.path_goal[0]) - float(previous[0]),
                float(state.path_goal[1]) - float(previous[1]),
            )
            current_distance = math.hypot(
                float(state.path_goal[0]) - float(position[0]),
                float(state.path_goal[1]) - float(position[1]),
            )
            goal_progress = previous_distance - current_distance

        uncommitted_motion = (
            route_length <= 1e-6
            and state.path_goal is None
            and math.hypot(move_x, move_y) >= 0.35
        )
        if state.path_goal is not None:
            strategic_distance = math.hypot(
                float(state.path_goal[0]) - float(position[0]),
                float(state.path_goal[1]) - float(position[1]),
            )
            if state.strategic_goal_distance is None:
                state.strategic_goal_distance = strategic_distance
                state.strategic_progress_at = now
            elif strategic_distance <= state.strategic_goal_distance - 3.0:
                state.strategic_goal_distance = strategic_distance
                state.strategic_progress_at = now
            regional_anchor = state.regional_progress_anchor
            if regional_anchor is None:
                state.regional_progress_anchor = position
                state.regional_progress_at = now
            elif math.hypot(
                float(position[0]) - float(regional_anchor[0]),
                float(position[1]) - float(regional_anchor[1]),
            ) >= _REGIONAL_PROGRESS_DISTANCE:
                state.regional_progress_anchor = position
                state.regional_progress_at = now

        if (
            forward_progress >= 0.2
            or goal_progress >= 0.2
            or uncommitted_motion
        ):
            state.last_progress_at = now
            strategic_stalled = (
                state.path_goal is not None
                and state.strategic_progress_at > 0.0
                and now - state.strategic_progress_at >= 6.0
            )
            regional_stalled = (
                state.path_goal is not None
                and state.regional_progress_at > 0.0
                and now - state.regional_progress_at
                >= _REGIONAL_STALL_SECONDS
            )
            if not strategic_stalled and not regional_stalled:
                state.next_stuck_recovery_at = 0.0
                state.stuck_attempts = 0
                if state.route_escape_goal is None:
                    state.route_escape_failures = 0
        return False

    @staticmethod
    def _consume_action_feedback(
        observer: PlayerSnapshot, state: _BrainState, now: float
    ) -> None:
        """Back off and replan after rejected construction or deployment."""

        feedback_frame = int(observer.last_action_frame)
        if feedback_frame <= state.last_action_feedback_frame:
            return
        state.last_action_feedback_frame = feedback_frame
        if bool(observer.last_action_accepted):
            return
        if str(observer.last_action_kind) not in {
            BotActionKind.BUILD.value,
            BotActionKind.BUILD_LINE.value,
            BotActionKind.PLACE_PREFAB.value,
            BotActionKind.DEPLOY.value,
        }:
            return
        # Allow normal combat/navigation immediately, but embargo every world
        # action generator long enough for topology and reservations to move
        # on. This breaks the stare/retry loop without muting valid gunfire.
        retry_at = now + 2.5
        state.next_cover_build_at = max(state.next_cover_build_at, retry_at)
        state.next_world_action_at = max(state.next_world_action_at, retry_at)
        state.next_traversal_prefab_at = max(
            state.next_traversal_prefab_at, retry_at
        )
        state.next_fortify_build_at = max(
            state.next_fortify_build_at, retry_at
        )
        state.next_zombie_build_at = max(state.next_zombie_build_at, retry_at)
        state.next_breach_at = max(state.next_breach_at, retry_at)
        state.path.clear()
        state.path_goal = None
        state.path_topology_version = -1
        state.next_path_request_at = 0.0
        state.escape_build_cell = None
        state.escape_build_until = 0.0
        state.route_escape_goal = None
        state.route_escape_until = 0.0
        state.route_escape_started_at = 0.0
        state.stuck_attempts = min(8, state.stuck_attempts + 1)

    def _water_recovery(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
    ) -> BotIntent | None:
        """Prioritize a bounded dry exit whenever native physics reports wade."""

        if not observer.wade:
            return None
        # Authored seas may contain tens of thousands of connected columns.
        # Resume that global search in small slices so twelve swimmers cannot
        # pin the single decision worker and starve every bot heartbeat.
        step = self.world.action_planner.water_exit(
            observer.position,
            max_nodes=128,
        )
        if step is None:
            return None
        state.last_path_direction = step.direction
        state.last_affordance = step.affordance
        state.path_goal = step.goal
        state.path_topology_version = int(frame.topology_version)
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                direction=step.direction,
                jump=True,
                sprint=False,
                affordance=step.affordance,
            ),
            look=LookIntent(step.waypoint, visible=False),
            priority=BotIntentPriority.SURVIVAL,
            debug_role="water_recovery",
        )

    @staticmethod
    def _preferred_melee_tool(observer: PlayerSnapshot) -> int | None:
        """Choose the strongest selected terrain/melee fallback."""

        selected = {
            int(tool)
            for tool in observer.loadout
            if int(tool) in {int(value) for value in C.ALL_MELEE_WEAPONS}
        }
        for preferred in (
            int(C.SUPERSPADE_TOOL),
            int(getattr(C, "UGC_SUPERSPADE_TOOL", -1)),
            int(C.ZOMBIEHAND_TOOL),
            int(C.SPADE_TOOL),
            int(getattr(C, "MACHETE_TOOL", -1)),
            int(getattr(C, "PICKAXE_TOOL", -1)),
        ):
            if preferred in selected:
                return preferred
        return min(selected) if selected else None

    @staticmethod
    def _select_prefab(
        prefabs: Iterable[str],
        *,
        purpose: str,
        selector: int = 0,
        block_budget: int | None = None,
    ) -> str | None:
        """Choose a prefab whose authored name matches the tactical need."""

        tokens = {
            "traversal": _TRAVERSAL_PREFAB_TOKENS,
            "climb": _CLIMB_PREFAB_TOKENS,
            "cover": _COVER_PREFAB_TOKENS,
        }.get(str(purpose), _COVER_PREFAB_TOKENS)
        ranked: list[tuple[int, int, str, str]] = []
        for value in prefabs:
            name = str(value)
            normalized = name.lower()
            if not bot_prefab_is_suitable(normalized, purpose):
                continue
            block_count = bot_prefab_block_count(normalized)
            if block_budget is not None and (
                block_count is None or block_count > int(block_budget)
            ):
                continue
            rank = next(
                (index for index, token in enumerate(tokens) if token in normalized),
                None,
            )
            if rank is not None:
                ranked.append((rank, len(normalized), normalized, name))
        if not ranked:
            return None
        ranked.sort()
        # Rotate among equally useful authored options without ever selecting
        # decorative props for a bridge or cover request.
        best_rank = ranked[0][0]
        candidates = [row[3] for row in ranked if row[0] == best_rank]
        return candidates[int(selector) % len(candidates)]

    @staticmethod
    def _navigation_look(
        observer: PlayerSnapshot,
        direction: Vector3,
        goal: Vector3,
    ) -> Vector3:
        """Look along the immediate route instead of through distant terrain."""

        normalized = _normalized_xy(direction[0], direction[1])
        if math.hypot(normalized[0], normalized[1]) <= 0.1:
            normalized = _normalized_xy(
                goal[0] - observer.position[0],
                goal[1] - observer.position[1],
            )
        if math.hypot(normalized[0], normalized[1]) <= 0.1:
            normalized = _normalized_xy(
                observer.orientation[0], observer.orientation[1]
            )
        planar_goal = math.hypot(
            goal[0] - observer.position[0],
            goal[1] - observer.position[1],
        )
        distance = min(10.0, max(4.0, planar_goal))
        # Strategic anchors can sit on another vertical layer. A travel gaze
        # only needs the next few blocks; cap pitch so a blocked route does
        # not present as a bot staring into the sky or straight at the floor.
        vertical = max(-2.0, min(2.0, goal[2] - observer.eye[2]))
        return (
            observer.eye[0] + normalized[0] * distance,
            observer.eye[1] + normalized[1] * distance,
            observer.eye[2] + vertical,
        )

    def _tactical_goal(
        self,
        observer: PlayerSnapshot,
        state: _BrainState,
        decision: ModeBotDecision,
        now: float,
    ) -> Vector3 | None:
        """Shift a holding/assault goal onto nearby commanding terrain.

        Only replaces the policy goal when a neighborhood cell is at least
        two blocks higher than the goal's own cell, snaps to a real standing
        node, and is reachable through bounded walk/jump topology.  Cached
        per bot for several seconds so goals stay steady.
        """

        if now < state.tactical_goal_until:
            return state.tactical_goal
        state.tactical_goal_until = now + self._rng.uniform(6.0, 10.0)
        state.tactical_goal = None
        tactical = getattr(self.world, "tactical", None)
        if tactical is None:
            return None
        candidate = tactical.high_ground_near(decision.position, radius_cells=2)
        if candidate is None:
            return None
        goal_cell = tactical.cell_at(decision.position)
        goal_mean = float(goal_cell[0]) if goal_cell is not None else 240.0
        if candidate[2] + 2.25 > goal_mean - 2.0:
            # Not meaningfully higher than the terrain already at the goal.
            return None
        node = self.world._standing_node(
            int(candidate[0]), int(candidate[1]), candidate[2], vertical_span=8
        )
        if node is None:
            return None
        site = (float(node[0]), float(node[1]), float(node[2]) - 2.25)
        if not self._fortify_reachable(observer.position, site, node[2]):
            return None
        state.tactical_goal = _formation_point(
            (site[0] + 0.5, site[1] + 0.5, site[2]),
            observer.player_id,
            3.0,
        )
        return state.tactical_goal

    # --- fortification -----------------------------------------------------

    _FORTIFY_RING = 3
    _FORTIFY_BEARINGS = tuple(
        index * math.tau / 8.0 for index in range(8)
    )

    def _fortify_site(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        now: float,
    ) -> Vector3 | None:
        """Score one shared defensible site per team near its anchor.

        Elevation (z-down: smaller support = higher ground), few walkable
        approaches, and teammate proximity score up; distance from the
        anchor scores mildly down.  Bounded: 48 candidates on a ring grid,
        recomputed at most every five seconds per team.
        """

        anchor = next(
            (
                item
                for item in frame.objectives
                if item.kind == "team_anchor" and item.team == observer.team
            ),
            None,
        )
        if anchor is None:
            return None
        key = (int(frame.map_epoch), int(observer.team))
        cached = self._fortify_sites.get(key)
        if cached is not None and now - cached[1] < 5.0:
            return cached[0]
        base = anchor.position
        base_node = self.world._standing_node(
            int(round(base[0])), int(round(base[1])), base[2], vertical_span=8
        )
        if base_node is None:
            return None
        base_support = base_node[2]
        teammates = [
            player
            for player in frame.players
            if player.team == observer.team and player.alive and player.spawned
        ]
        candidates: list[tuple[float, Vector3, int]] = []
        for radius in (4.0, 8.0, 12.0, 16.0, 20.0, 24.0):
            for angle in self._FORTIFY_BEARINGS:
                x = int(round(base[0] + math.cos(angle) * radius))
                y = int(round(base[1] + math.sin(angle) * radius))
                if not (8 <= x < 504 and 8 <= y < 504):
                    continue
                node = self.world._standing_node(
                    x, y, base[2], vertical_span=8
                )
                if node is None:
                    continue
                support = node[2]
                player_z = float(support) - 2.25
                elevation = max(0.0, min(8.0, float(base_support - support)))
                open_count = len(self._open_approaches(node))
                nearby = sum(
                    1
                    for player in teammates
                    if _distance_squared(
                        player.position, (float(x), float(y), player_z)
                    )
                    <= 8.0 ** 2
                )
                score = (
                    2.0 * elevation
                    + 3.0 * (8.0 - float(open_count))
                    + 1.0 * nearby
                    - 0.15 * radius
                )
                candidates.append(
                    (score, (float(x), float(y), player_z), support)
                )
        # Unreachable spires otherwise dominate (max elevation, zero
        # approaches): accept the best candidate the team can actually walk
        # to from its anchor, checking only a bounded few.
        candidates.sort(key=lambda row: row[0], reverse=True)
        anchor_start = (base[0], base[1], float(base_support) - 2.25)
        best: Vector3 | None = None
        for score, position, support in candidates[:6]:
            if self._fortify_reachable(anchor_start, position, support):
                best = position
                break
        if best is None:
            best = tuple(float(value) for value in base)
        self._fortify_sites[key] = (best, now)
        return best

    def _fortify_reachable(
        self, from_position: Vector3, site: Vector3, support: int
    ) -> bool:
        """True when bounded walk/jump topology actually reaches the site."""

        astar = getattr(self.world, "_a_star", None)
        if not callable(astar):
            return True
        path, _affordance = astar(
            from_position,
            site,
            abilities=frozenset(
                {MovementAffordance.WALK, MovementAffordance.JUMP}
            ),
        )
        if not path:
            return False
        end = path[-1]
        return (
            math.hypot(end[0] - (site[0] + 0.5), end[1] - (site[1] + 0.5))
            <= 1.6
            and abs((end[2] + 2.25) - float(support)) <= 1.5
        )

    def _wall_segment_cells(
        self, x: int, y: int, angle: float
    ) -> list[tuple[int, int]]:
        """Return the three ring columns sealing one compass approach."""

        center_x = x + int(round(math.cos(angle) * self._FORTIFY_RING))
        center_y = y + int(round(math.sin(angle) * self._FORTIFY_RING))
        perp_x, perp_y = -math.sin(angle), math.cos(angle)
        cells: list[tuple[int, int]] = []
        for offset in (-1, 0, 1):
            wx = center_x + int(round(perp_x * offset))
            wy = center_y + int(round(perp_y * offset))
            if (wx, wy) not in cells and 1 <= wx < 511 and 1 <= wy < 511:
                cells.append((wx, wy))
        return cells

    def _open_approaches(
        self, site_node: tuple[int, int, int]
    ) -> list[float]:
        """Bearings whose wall segment is still walkable into the site."""

        x, y, support = site_node
        player_z = float(support) - 2.25
        open_bearings: list[float] = []
        for angle in self._FORTIFY_BEARINGS:
            for wx, wy in self._wall_segment_cells(x, y, angle):
                node = self.world._standing_node(
                    wx, wy, player_z, vertical_span=2
                )
                if node is not None and abs(node[2] - support) <= 1:
                    open_bearings.append(angle)
                    break
        return open_bearings

    @staticmethod
    def _sealable_approaches(
        open_angles: list[float], keep_door: bool
    ) -> list[float]:
        """Apply the door rule: never seal the last way in while it matters."""

        if not keep_door:
            return list(open_angles)
        if len(open_angles) <= 1:
            return []
        return list(open_angles[:-1])

    def _fortify_floor(self, wx: int, wy: int, support: int) -> int | None:
        """Topmost solid z of a wall column near the site plane (z-down)."""

        for wz in range(max(2, support - 4), min(238, support + 5)):
            if self.world.solid(wx, wy, wz):
                return wz
        return None

    @staticmethod
    def _column_occupied_by_team(
        frame: PerceptionFrame, observer: PlayerSnapshot, wx: int, wy: int
    ) -> bool:
        for player in frame.players:
            if player.team != observer.team or not player.alive:
                continue
            if (
                int(math.floor(player.position[0])) == wx
                and int(math.floor(player.position[1])) == wy
            ):
                return True
        return False

    def _fortify_build_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        site: Vector3,
        now: float,
    ) -> BotIntent | None:
        """Place the next missing barricade block around the fortify site.

        One block per decision at a human cadence.  Wall height converges to
        two blocks above the site plane; construction reservations and
        can_build/_block_supported re-validate everything authoritatively.
        """

        if now < state.next_fortify_build_at:
            return None
        if int(C.BLOCK_TOOL) not in observer.loadout or observer.blocks <= 8:
            return None
        x, y = int(round(site[0])), int(round(site[1]))
        site_node = self.world._standing_node(x, y, site[2], vertical_span=8)
        if site_node is None:
            return None
        support = site_node[2]
        open_angles = self._open_approaches(site_node)
        phase = str(frame.mode_phase).lower()
        keep_door = phase in ("", "waiting") and any(
            player.team == observer.team
            and player.alive
            and player.player_id != observer.player_id
            and _distance_squared(player.position, site) > 6.0 ** 2
            for player in frame.players
        )
        sealable = self._sealable_approaches(open_angles, keep_door)
        # Wall the approach nearest to this builder first.
        sealable.sort(
            key=lambda angle: (
                (x + math.cos(angle) * self._FORTIFY_RING - observer.position[0]) ** 2
                + (y + math.sin(angle) * self._FORTIFY_RING - observer.position[1]) ** 2
            )
        )
        for angle in sealable:
            for wx, wy in self._wall_segment_cells(x, y, angle):
                floor = self._fortify_floor(wx, wy, support)
                if floor is None or floor <= support - 2:
                    # No reachable floor, or nature already walls this cell.
                    continue
                if self._column_occupied_by_team(frame, observer, wx, wy):
                    continue
                for wz in (floor - 1, floor - 2):
                    if wz < max(2, support - 2) or wz > 237:
                        continue
                    if self.world.solid(wx, wy, wz):
                        continue
                    state.next_fortify_build_at = now + self._rng.uniform(
                        0.6, 1.0
                    )
                    return self._intent(
                        frame,
                        now,
                        movement=MovementIntent(crouch=True),
                        look=LookIntent(
                            (wx + 0.5, wy + 0.5, wz + 0.5), visible=False
                        ),
                        tool_id=int(C.BLOCK_TOOL),
                        action=BotAction(
                            BotActionKind.BUILD,
                            tool_id=int(C.BLOCK_TOOL),
                            position=(float(wx), float(wy), float(wz)),
                        ),
                        debug_role="fortify_build",
                    )
        return None

    def _fortify_hold_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        site: Vector3,
        now: float,
    ) -> BotIntent:
        """Hold the fort, slowly scanning across the open approaches."""

        x, y = int(round(site[0])), int(round(site[1]))
        site_node = self.world._standing_node(x, y, site[2], vertical_span=8)
        open_angles = (
            self._open_approaches(site_node) if site_node is not None else []
        )
        if open_angles:
            angle = open_angles[
                int(now * 0.35 + observer.player_id) % len(open_angles)
            ]
        else:
            angle = (
                float(observer.player_id) * (math.tau / 8.0) + now * 0.2
            ) % math.tau
        look = (
            site[0] + math.cos(angle) * 12.0,
            site[1] + math.sin(angle) * 12.0,
            site[2],
        )
        return self._intent(
            frame,
            now,
            movement=MovementIntent(crouch=True),
            look=LookIntent(look, visible=False),
            debug_role="fortify_hold",
        )

    def _blast_retreat(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
    ) -> BotIntent | None:
        """Keep clear of an explosive this bot armed until it detonates."""

        if state.retreat_until <= now or state.retreat_from is None:
            state.retreat_from = None
            return None
        source = state.retreat_from
        if _distance_squared(observer.position, source) >= 12.0 ** 2:
            # Far enough: hold and watch the charge instead of wandering back.
            return self._intent(
                frame,
                now,
                movement=MovementIntent(crouch=True),
                look=LookIntent(source, visible=False),
                priority=BotIntentPriority.SURVIVAL,
                debug_role="blast_overwatch",
            )
        away = _normalized_xy(
            observer.position[0] - source[0],
            observer.position[1] - source[1],
        )
        if math.hypot(away[0], away[1]) <= 0.1:
            away = (
                math.cos(state.patrol_heading),
                math.sin(state.patrol_heading),
                0.0,
            )
        return self._intent(
            frame,
            now,
            movement=MovementIntent(direction=away, sprint=True),
            look=LookIntent(source, visible=False),
            priority=BotIntentPriority.SURVIVAL,
            debug_role="blast_retreat",
        )

    def _active_hazard_retreat(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
    ) -> BotIntent | None:
        """Escape every live blast volume, regardless of who armed it."""

        nearby = [
            entity
            for entity in frame.entities
            if entity.alive
            and entity.hazardous
            and entity.blast_radius > 0.0
            and _distance_squared(observer.position, entity.position)
            <= (float(entity.blast_radius) + 2.0) ** 2
        ]
        if not nearby:
            return None
        away_x = 0.0
        away_y = 0.0
        for entity in nearby:
            dx = observer.position[0] - entity.position[0]
            dy = observer.position[1] - entity.position[1]
            distance = max(0.5, math.hypot(dx, dy))
            weight = (float(entity.blast_radius) + 2.0) / (distance * distance)
            away_x += dx * weight
            away_y += dy * weight
        away = _normalized_xy(away_x, away_y)
        if math.hypot(away[0], away[1]) <= 0.1:
            away = (
                math.cos(state.patrol_heading),
                math.sin(state.patrol_heading),
                0.0,
            )
        escape_distance = max(float(item.blast_radius) for item in nearby) + 4.0
        goal = (
            observer.position[0] + away[0] * escape_distance,
            observer.position[1] + away[1] * escape_distance,
            observer.position[2],
        )
        movement = self._path_direction(
            observer.position,
            goal,
            state,
            now,
            agent_id=observer.player_id,
            velocity=observer.velocity,
            abilities=self._movement_abilities(observer),
        )
        if math.hypot(movement[0], movement[1]) <= 0.1:
            movement = away
        nearest = min(
            nearby,
            key=lambda entity: _distance_squared(
                observer.position, entity.position
            ),
        )
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                direction=movement,
                sprint=True,
                affordance=state.last_affordance,
            ),
            look=LookIntent(nearest.position, visible=False),
            priority=BotIntentPriority.SURVIVAL,
            debug_role="explosive_hazard_escape",
        )

    def _damage_reaction(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
    ) -> BotIntent | None:
        """Interrupt low-priority work after a recent non-visible hit."""

        source = observer.last_damage_source_position
        if (
            source is None
            or observer.last_damage_source_id < 0
            or now - float(observer.last_damage_at) > 2.0
        ):
            return None
        cover_reader = getattr(self.world, "cover_direction", None)
        movement = (
            cover_reader(observer.position, source)
            if callable(cover_reader)
            else (0.0, 0.0, 0.0)
        )
        if math.hypot(movement[0], movement[1]) <= 0.1:
            away = _normalized_xy(
                observer.position[0] - source[0],
                observer.position[1] - source[1],
            )
            if math.hypot(away[0], away[1]) <= 0.1:
                away = (
                    math.cos(state.patrol_heading),
                    math.sin(state.patrol_heading),
                    0.0,
                )
            goal = (
                observer.position[0] + away[0] * 10.0,
                observer.position[1] + away[1] * 10.0,
                observer.position[2],
            )
            movement = self._path_direction(
                observer.position,
                goal,
                state,
                now,
                agent_id=observer.player_id,
                velocity=observer.velocity,
                abilities=self._movement_abilities(observer),
            )
            if math.hypot(movement[0], movement[1]) <= 0.1:
                movement = away
        weapon_tool = (
            int(observer.weapon_tool)
            if int(observer.weapon_tool) in observer.loadout
            else -1
        )
        action = BotAction()
        if (
            weapon_tool >= 0
            and observer.ammo_clip <= 0
            and observer.ammo_reserve > 0
            and not observer.reloading
        ):
            action = BotAction(BotActionKind.RELOAD, tool_id=weapon_tool)
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                direction=movement,
                sprint=True,
                affordance=state.last_affordance,
            ),
            look=LookIntent(source, visible=False),
            tool_id=weapon_tool,
            action=action,
            priority=BotIntentPriority.COMBAT,
            debug_role="damage_reaction",
        )

    @staticmethod
    def _explosive_target_safe(
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        position: Vector3,
        tool_id: int,
        *,
        ignore_observer: bool,
    ) -> bool:
        """Check friendly bodies and already-live blast volumes at a target."""

        spec = PROJECTILE_SPECS.get(int(tool_id))
        fallback = {
            int(C.DYNAMITE_TOOL): float(
                getattr(C, "DYNAMITE_EXPLOSION_RADIUS", 5.0)
            ),
            int(C.LANDMINE_TOOL): float(
                getattr(C, "LANDMINE_EXPLOSION_RADIUS", 3.0)
            ),
            int(C.C4_TOOL): float(getattr(C, "C4_EXPLOSION_RADIUS", 8.0)),
        }.get(int(tool_id), 0.0)
        radius = max(
            fallback,
            float(getattr(spec, "blast_radius", 0.0) or 0.0),
        )
        if radius <= 0.0:
            return True
        for player in frame.players:
            if player.team != observer.team or not player.alive or not player.spawned:
                continue
            if ignore_observer and player.player_id == observer.player_id:
                continue
            if _distance_squared(player.position, position) <= (radius + 1.5) ** 2:
                return False
        for entity in frame.entities:
            if not entity.alive or not entity.hazardous:
                continue
            if _distance_squared(entity.position, position) <= (
                radius + float(entity.blast_radius) + 1.0
            ) ** 2:
                return False
        return True

    @staticmethod
    def _friendly_launch_lane_clear(
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        target: Vector3,
    ) -> bool:
        """Reject an oriented explosive when a teammate crosses its lane."""

        start = observer.eye
        delta = tuple(target[index] - start[index] for index in range(3))
        length_squared = sum(value * value for value in delta)
        if length_squared <= 1e-6:
            return False
        for player in frame.players:
            if (
                player.player_id == observer.player_id
                or player.team != observer.team
                or not player.alive
                or not player.spawned
            ):
                continue
            relative = tuple(
                player.eye[index] - start[index] for index in range(3)
            )
            fraction = sum(
                relative[index] * delta[index] for index in range(3)
            ) / length_squared
            if not 0.03 < fraction < 0.97:
                continue
            closest = tuple(
                start[index] + delta[index] * fraction for index in range(3)
            )
            if _distance_squared(player.eye, closest) <= 2.25 ** 2:
                return False
        return True

    def _oriented_attack_utility(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        target: PlayerSnapshot,
        tool_id: int,
    ) -> float:
        """Score a selected explosive for this visible engagement."""

        spec = PROJECTILE_SPECS.get(int(tool_id))
        if spec is None:
            return 0.0
        distance = math.hypot(
            target.position[0] - observer.position[0],
            target.position[1] - observer.position[1],
        )
        radius = max(0.0, float(getattr(spec, "blast_radius", 0.0)))
        minimum = max(
            radius + 4.0,
            15.0 if spec.behavior in {"bounce", "stick", "deploy"} else 10.0,
        )
        maximum = {
            "bounce": 50.0,
            "stick": 55.0,
            "deploy": 45.0,
            "contact": 90.0,
        }.get(str(spec.behavior), 60.0)
        if not minimum <= distance <= maximum:
            return 0.0
        if not self._explosive_target_safe(
            frame,
            observer,
            target.position,
            int(tool_id),
            ignore_observer=False,
        ):
            return 0.0
        if not self._friendly_launch_lane_clear(
            frame, observer, target.position
        ):
            return 0.0
        cluster = sum(
            1
            for player in frame.players
            if player.team != observer.team
            and player.alive
            and player.spawned
            and _distance_squared(player.position, target.position)
            <= (radius + 2.0) ** 2
        )
        # Do not spend scarce equipment finishing a nearly-dead isolated
        # opponent; ordinary gunfire is safer and faster in that case.
        if cluster <= 1 and int(target.health) <= 25:
            return 0.0
        damage = min(250.0, max(0.0, float(getattr(spec, "damage", 0.0))))
        utility = 0.5 + damage / 250.0 + radius / 12.0 + max(0, cluster - 1)
        target_speed = math.sqrt(sum(value * value for value in target.velocity))
        if spec.behavior == "bounce" and target_speed > 4.0:
            utility *= 0.65
        return utility

    def _medic_support_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
    ) -> BotIntent | None:
        """Move to the nearest wounded teammate and drop a medpack.

        Own-roster health is sanctioned team knowledge; no enemy state is
        read here.  The deployable service re-validates stock and placement.
        """

        if int(observer.class_id) != int(C.CLASS_MEDIC):
            return None
        if int(C.MEDPACK_TOOL) not in observer.loadout:
            return None
        if now < state.next_medic_support_at:
            return None
        wounded = [
            player
            for player in frame.players
            if player.team == observer.team
            and player.player_id != observer.player_id
            and player.alive
            and player.spawned
            and player.health < 60
            and _distance_squared(observer.position, player.position)
            <= 30.0 ** 2
        ]
        if not wounded:
            return None
        patient = min(
            wounded,
            key=lambda player: (
                player.health,
                _distance_squared(observer.position, player.position),
            ),
        )
        if _distance_squared(observer.position, patient.position) <= 4.0 ** 2:
            state.next_medic_support_at = now + 6.0
            return self._intent(
                frame,
                now,
                movement=MovementIntent(crouch=True),
                look=LookIntent(patient.position, visible=False),
                tool_id=int(C.MEDPACK_TOOL),
                action=BotAction(
                    BotActionKind.DEPLOY,
                    tool_id=int(C.MEDPACK_TOOL),
                    position=patient.position,
                ),
                debug_role="medic_support",
            )
        movement = self._path_direction(
            observer.position,
            patient.position,
            state,
            now,
            agent_id=observer.player_id,
            velocity=observer.velocity,
            abilities=self._movement_abilities(observer),
        )
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                direction=movement,
                sprint=True,
                affordance=state.last_affordance,
            ),
            look=LookIntent(
                self._navigation_look(
                    observer, movement, patient.position
                ),
                visible=False,
            ),
            debug_role="medic_support",
        )

    def _maintenance_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        now: float,
    ) -> BotIntent | None:
        """Reload a dry firearm before low-urgency travel or construction."""

        weapon_tool = int(observer.weapon_tool)
        if (
            int(observer.class_id) in _ZOMBIE_CLASSES
            or weapon_tool not in observer.loadout
            or observer.reloading
            or observer.ammo_clip > 0
            or observer.ammo_reserve <= 0
        ):
            return None
        return self._intent(
            frame,
            now,
            movement=MovementIntent(crouch=True),
            look=None,
            tool_id=weapon_tool,
            action=BotAction(BotActionKind.RELOAD, tool_id=weapon_tool),
            debug_role="maintenance_reload",
        )

    def _miner_demolition(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        contact,
        now: float,
        direction: Vector3,
    ) -> BotIntent | None:
        """Blow a wall between a Miner and a stale contact, then retreat."""

        if int(observer.class_id) != int(C.CLASS_MINER):
            return None
        if int(C.DYNAMITE_TOOL) not in observer.loadout:
            return None
        if now < state.next_dynamite_at or now - contact.seen_at < 2.0:
            return None
        if math.hypot(direction[0], direction[1]) <= 0.1:
            return None
        blocking_reader = getattr(self.world, "blocking_cell", None)
        blocking = (
            blocking_reader(observer.position, direction)
            if callable(blocking_reader)
            else None
        )
        if blocking is None:
            return None
        target = tuple(float(value) + 0.5 for value in blocking)
        if not self._explosive_target_safe(
            frame,
            observer,
            target,
            int(C.DYNAMITE_TOOL),
            ignore_observer=True,
        ):
            state.next_dynamite_at = now + 2.5
            return None
        state.next_dynamite_at = now + 15.0
        fuse = float(getattr(C, "DYNAMITE_FUSE_TIME", 7.0))
        state.retreat_from = target
        state.retreat_until = now + fuse + 1.0
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                crouch=True, affordance=MovementAffordance.BREACH
            ),
            look=LookIntent(target, visible=False),
            tool_id=int(C.DYNAMITE_TOOL),
            action=BotAction(
                BotActionKind.DEPLOY,
                tool_id=int(C.DYNAMITE_TOOL),
                position=target,
            ),
            debug_role="miner_demolition",
        )

    def _proactive_breach(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
        direction: Vector3,
    ) -> BotIntent | None:
        """Let mining classes attack an immediate route obstruction early."""

        breach_classes = {
            int(C.CLASS_MINER),
            int(C.CLASS_ZOMBIE),
            int(C.CLASS_FAST_ZOMBIE),
            int(C.CLASS_JUMP_ZOMBIE),
        }
        if (
            int(observer.class_id) not in breach_classes
            or now < state.next_breach_at
            or math.hypot(direction[0], direction[1]) <= 0.1
        ):
            return None
        blocking_reader = getattr(self.world, "blocking_cell", None)
        blocking = (
            blocking_reader(observer.position, direction)
            if callable(blocking_reader)
            else None
        )
        melee = self._preferred_melee_tool(observer)
        if blocking is None or melee is None:
            return None
        interval = (
            float(getattr(C, "ZOMBIEHAND_SHOOT_INTERVAL", 0.4))
            if int(observer.class_id) in _ZOMBIE_CLASSES
            else 0.55
        )
        state.next_breach_at = now + interval
        target = tuple(float(value) + 0.5 for value in blocking)
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                crouch=True,
                affordance=MovementAffordance.BREACH,
            ),
            look=LookIntent(target, visible=False),
            tool_id=melee,
            action=BotAction(
                BotActionKind.MELEE,
                tool_id=melee,
                position=target,
            ),
            debug_role=(
                "zombie_fast_breach"
                if int(observer.class_id) in _ZOMBIE_CLASSES
                else "proactive_breach"
            ),
        )

    def _zombie_hunt_breach(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        objective: Vector3,
        now: float,
    ) -> BotIntent | None:
        """Claw the terrain hiding a nearby hunt marker from an infected bot.

        Fires only near the sanctioned marker: the first solid cell along the
        eye line within claw range becomes the dig target, so the horde eats
        through cover instead of orbiting it blindly.
        """

        if int(observer.class_id) not in _ZOMBIE_CLASSES:
            return None
        if now < state.next_breach_at:
            return None
        melee = self._preferred_melee_tool(observer)
        if melee is None:
            return None
        planar = math.hypot(
            objective[0] - observer.position[0],
            objective[1] - observer.position[1],
        )
        if planar > 8.0 or planar <= 1e-6:
            return None
        origin = observer.eye
        dx = objective[0] - origin[0]
        dy = objective[1] - origin[1]
        dz = objective[2] - origin[2]
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        if distance <= 1e-6:
            return None
        reach = min(3.5, distance)
        steps = max(2, int(math.ceil(reach * 2.0)))
        cell = None
        for index in range(1, steps + 1):
            fraction = (index / steps) * (reach / distance)
            x = int(math.floor(origin[0] + dx * fraction))
            y = int(math.floor(origin[1] + dy * fraction))
            z = int(math.floor(origin[2] + dz * fraction))
            if self.world.solid(x, y, z):
                cell = (x, y, z)
                break
        if cell is None:
            return None
        interval = (
            float(getattr(C, "ZOMBIEHAND_SHOOT_INTERVAL", 0.4))
            if int(observer.class_id) in _ZOMBIE_CLASSES
            else 0.55
        )
        state.next_breach_at = now + interval
        target = tuple(float(value) + 0.5 for value in cell)
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                crouch=True, affordance=MovementAffordance.BREACH
            ),
            look=LookIntent(target, visible=False),
            tool_id=melee,
            action=BotAction(
                BotActionKind.MELEE,
                tool_id=melee,
                position=target,
            ),
            debug_role="zombie_hunt_breach",
        )

    def _traversal_prefab_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
        direction: Vector3,
        prefab_name: str | None,
        *,
        role: str,
    ) -> BotIntent | None:
        """Place one semantically matched bridge/climb prefab when stalled."""

        prefab_cost = (
            bot_prefab_block_count(prefab_name)
            if prefab_name is not None
            else None
        )
        if (
            prefab_name is None
            or prefab_cost is None
            or prefab_cost > int(observer.blocks)
            or now < state.next_traversal_prefab_at
        ):
            return None
        normalized = _normalized_xy(direction[0], direction[1])
        if math.hypot(normalized[0], normalized[1]) <= 0.1:
            return None
        distance = 4.0 if "bridge" in str(role) else 2.5
        target = (
            observer.position[0] + normalized[0] * distance,
            observer.position[1] + normalized[1] * distance,
            observer.position[2],
        )
        state.next_traversal_prefab_at = now + 4.0
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                crouch=True,
                affordance=(
                    MovementAffordance.BUILD_BRIDGE
                    if "bridge" in str(role)
                    else MovementAffordance.BUILD_STEP
                ),
            ),
            look=LookIntent(target, visible=False),
            tool_id=int(C.PREFAB_TOOL),
            action=BotAction(
                BotActionKind.PLACE_PREFAB,
                tool_id=int(C.PREFAB_TOOL),
                position=target,
                argument=str(prefab_name),
                yaw=math.atan2(normalized[1], normalized[0]),
            ),
            priority=BotIntentPriority.TRAVERSAL,
            debug_role=str(role),
        )

    def _stuck_recovery(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
    ) -> BotIntent | None:
        """Escape local topology stalls with bounded physical affordances.

        Recovery uses the same legal actions as a client: two-block jumps,
        spade breaches, BlockLine bridges, and a two-phase jump/build under the
        bot.  It never mutates worker VXL directly; accepted actions return as
        canonical world deltas before the next climb step is planned.
        """

        block_tool = int(C.BLOCK_TOOL)
        has_blocks = block_tool in observer.loadout and observer.blocks > 0
        can_prefab = int(C.PREFAB_TOOL) in observer.loadout and observer.blocks > 0
        bridge_prefab = (
            self._select_prefab(
                observer.prefabs,
                purpose="traversal",
                selector=observer.player_id + frame.frame_id,
                block_budget=observer.blocks,
            )
            if can_prefab
            else None
        )
        climb_prefab = (
            self._select_prefab(
                observer.prefabs,
                purpose="climb",
                selector=observer.player_id + frame.frame_id,
                block_budget=observer.blocks,
            )
            if can_prefab
            else None
        )
        pending_build = state.escape_build_cell
        solid_reader = getattr(self.world, "solid", None)
        if pending_build is not None:
            expired = now > state.escape_build_until
            committed = callable(solid_reader) and solid_reader(*pending_build)
            if expired or committed or not has_blocks:
                state.escape_build_cell = None
                state.escape_build_until = 0.0
            elif not observer.grounded:
                target = tuple(float(value) for value in pending_build)
                direction = state.escape_direction
                state.escape_build_cell = None
                state.escape_build_until = 0.0
                return self._intent(
                    frame,
                    now,
                    movement=MovementIntent(
                        direction=direction,
                        jump=True,
                        affordance=MovementAffordance.BUILD_STEP,
                    ),
                    look=LookIntent(target, visible=False),
                    tool_id=block_tool,
                    action=BotAction(
                        BotActionKind.BUILD,
                        tool_id=block_tool,
                        position=target,
                    ),
                    priority=BotIntentPriority.TRAVERSAL,
                    debug_role="hole_jump_build_place",
                )
            else:
                # Keep the jump phase coherent across worker frames until the
                # native grounded bit confirms that the body cleared the cell.
                return self._intent(
                    frame,
                    now,
                    movement=MovementIntent(
                        direction=state.escape_direction,
                        jump=True,
                        affordance=MovementAffordance.JUMP,
                    ),
                    look=None,
                    tool_id=block_tool,
                    priority=BotIntentPriority.TRAVERSAL,
                    debug_role="hole_jump_build_launch",
                )

        active_escape = self._active_route_escape(
            frame, observer, state, now
        )
        if active_escape is not None:
            return active_escape

        raw_direction = state.last_path_direction
        stalled = (
            (
                state.last_progress_at > 0.0
                and now - state.last_progress_at >= _STUCK_TRIGGER_SECONDS
            )
            or (
                state.path_goal is not None
                and state.strategic_progress_at > 0.0
                and now - state.strategic_progress_at >= 6.0
            )
            or (
                state.path_goal is not None
                and state.regional_progress_at > 0.0
                and now - state.regional_progress_at
                >= _REGIONAL_STALL_SECONDS
            )
        ) and now >= state.next_stuck_recovery_at
        if not stalled:
            return None
        direction = raw_direction
        if math.hypot(direction[0], direction[1]) <= 0.1:
            direction = (
                math.cos(state.patrol_heading),
                math.sin(state.patrol_heading),
                0.0,
            )

        overhead_reader = getattr(self.world, "overhead_block", None)
        hole_reader = getattr(self.world, "hole_escape", None)
        overhead = (
            overhead_reader(observer.position)
            if callable(overhead_reader)
            else None
        )
        hole = (
            hole_reader(observer.position, direction)
            if callable(hole_reader)
            else None
        )
        if (
            math.hypot(raw_direction[0], raw_direction[1]) <= 0.1
            and overhead is None
            and hole is None
            and state.path_goal is None
        ):
            # No route has requested progress yet. This occurs on the first
            # objective frame after a legitimate hold; wait for that branch to
            # establish its path goal before choosing an escape direction.
            return None

        # Pace attempts without claiming that an attempted jump/dig/build was
        # actual locomotion. Real progress is written only by
        # ``_record_progress`` from the next authoritative player snapshot.
        state.next_stuck_recovery_at = now + _STUCK_RETRY_SECONDS
        state.stuck_attempts += 1
        preferred = self._preferred_melee_tool(observer)
        if (
            overhead is not None
            and preferred is not None
            and state.stuck_attempts <= 2
        ):
            target = tuple(float(value) + 0.5 for value in overhead)
            return self._intent(
                frame,
                now,
                movement=MovementIntent(affordance=MovementAffordance.BREACH),
                look=LookIntent(target, visible=False),
                tool_id=preferred,
                action=BotAction(
                    BotActionKind.MELEE,
                    tool_id=preferred,
                    position=target,
                ),
                priority=BotIntentPriority.TRAVERSAL,
                debug_role="hole_break_ceiling",
            )

        if hole is not None:
            escape_direction, rise = hole
            direction = escape_direction
            if rise <= 2 and state.stuck_attempts <= 2:
                state.last_affordance = MovementAffordance.JUMP
                return self._intent(
                    frame,
                    now,
                    movement=MovementIntent(
                        direction=direction,
                        jump=True,
                        sprint=True,
                        affordance=MovementAffordance.JUMP,
                    ),
                    look=None,
                    priority=BotIntentPriority.TRAVERSAL,
                    debug_role="hole_two_block_jump",
                )
            jump_cell_reader = getattr(self.world, "jump_build_cell", None)
            jump_cell = (
                jump_cell_reader(observer.position)
                if has_blocks and callable(jump_cell_reader)
                else None
            )
            if jump_cell is not None and state.stuck_attempts <= 3:
                state.escape_build_cell = jump_cell
                state.escape_build_until = now + 1.25
                state.escape_direction = direction
                state.last_affordance = MovementAffordance.JUMP
                return self._intent(
                    frame,
                    now,
                    movement=MovementIntent(
                        direction=direction,
                        jump=True,
                        affordance=MovementAffordance.JUMP,
                    ),
                    look=None,
                    tool_id=block_tool,
                    priority=BotIntentPriority.TRAVERSAL,
                    debug_role="hole_jump_build_launch",
                )
            if rise > 2 and state.stuck_attempts <= 3:
                prefab_intent = self._traversal_prefab_intent(
                    frame,
                    observer,
                    state,
                    now,
                    direction,
                    climb_prefab,
                    role="hole_prefab_climb",
                )
                if prefab_intent is not None:
                    return prefab_intent

        blocking_reader = getattr(self.world, "blocking_cell", None)
        blocking = (
            blocking_reader(observer.position, direction)
            if callable(blocking_reader)
            else None
        )
        if blocking is not None and preferred is not None and state.stuck_attempts <= 3:
            target = tuple(float(value) + 0.5 for value in blocking)
            return self._intent(
                frame,
                now,
                movement=MovementIntent(
                    crouch=True, affordance=MovementAffordance.BREACH
                ),
                look=LookIntent(target, visible=False),
                tool_id=preferred,
                action=BotAction(
                    BotActionKind.MELEE,
                    tool_id=preferred,
                    position=target,
                ),
                priority=BotIntentPriority.TRAVERSAL,
                debug_role="stuck_side_breach",
            )

        line_reader = getattr(self.world, "water_bridge_line", None)
        center_line = (
            line_reader(
                observer.position,
                direction,
                max_cells=(
                    min(6, max(1, int(observer.blocks)))
                    if has_blocks
                    else 6
                ),
            )
            if (has_blocks or bridge_prefab is not None)
            and callable(line_reader)
            else None
        )
        line = center_line
        line_role = "water_gap_block_line"
        shoulder_reader = getattr(
            self.world, "narrow_bridge_shoulder_line", None
        )
        if line is None and has_blocks and callable(shoulder_reader):
            line = shoulder_reader(
                observer.position,
                direction,
                max_cells=min(6, max(1, int(observer.blocks))),
            )
            if line is not None:
                line_role = "water_gap_widen_block_line"
        if line is not None and has_blocks and state.stuck_attempts <= 3:
            start, end = line
            cost = max(
                abs(int(end[index]) - int(start[index])) for index in range(3)
            ) + 1
            if observer.blocks >= cost:
                return self._intent(
                    frame,
                    now,
                    movement=MovementIntent(
                        crouch=True,
                        affordance=MovementAffordance.BUILD_BRIDGE,
                    ),
                    look=LookIntent(tuple(float(value) for value in end), visible=False),
                    tool_id=block_tool,
                    action=BotAction(
                        BotActionKind.BUILD_LINE,
                        tool_id=block_tool,
                        position=tuple(float(value) for value in start),
                        end_position=tuple(float(value) for value in end),
                    ),
                    priority=BotIntentPriority.TRAVERSAL,
                    debug_role=line_role,
                )
        if center_line is not None and state.stuck_attempts <= 3:
            prefab_intent = self._traversal_prefab_intent(
                frame,
                observer,
                state,
                now,
                direction,
                bridge_prefab,
                role="water_gap_prefab_bridge",
            )
            if prefab_intent is not None:
                return prefab_intent

        bridge_reader = getattr(self.world, "bridge_cell", None)
        bridge = (
            bridge_reader(observer.position, direction)
            if callable(bridge_reader)
            else None
        )
        if (
            bridge is not None
            and has_blocks
            and state.stuck_attempts <= 3
        ):
            target = tuple(float(value) for value in bridge)
            return self._intent(
                frame,
                now,
                movement=MovementIntent(
                    crouch=True, affordance=MovementAffordance.BUILD_BRIDGE
                ),
                look=LookIntent(target, visible=False),
                tool_id=block_tool,
                action=BotAction(
                    BotActionKind.BUILD,
                    tool_id=block_tool,
                    position=target,
                ),
                priority=BotIntentPriority.TRAVERSAL,
                debug_role="single_gap_bridge",
            )
        if bridge is not None and state.stuck_attempts <= 3:
            prefab_intent = self._traversal_prefab_intent(
                frame,
                observer,
                state,
                now,
                direction,
                bridge_prefab,
                role="single_gap_prefab_bridge",
            )
            if prefab_intent is not None:
                return prefab_intent
        zombie_prefabs = tuple(
            name
            for name in observer.prefabs
            if "zombie" in name.lower()
            and (bot_prefab_block_count(name) or math.inf) <= observer.blocks
        )
        if (
            int(observer.class_id) in _ZOMBIE_CLASSES
            and zombie_prefabs
            and int(C.ZOMBIE_PREFAB_TOOL) in observer.loadout
            and observer.blocks >= 100
            and state.stuck_attempts <= 3
            and now >= state.next_zombie_build_at
        ):
            # Zombie classes do not own the ordinary block tool. Their native
            # construction affordance is tool 28 plus hand/bone/head prefabs.
            # The upright hand is the first climbing step; after another
            # failed route the long bone becomes a bridge/ramp. The gameplay
            # service rechecks stock, collision, support, and protected zones.
            preferred_name = (
                "prefab_zombiebone"
                if state.stuck_attempts >= 2
                else "prefab_zombiehand"
            )
            zombie_prefab = next(
                (
                    name for name in zombie_prefabs
                    if name.lower() == preferred_name
                ),
                zombie_prefabs[0],
            )
            target = (
                observer.position[0] + direction[0] * 4.0,
                observer.position[1] + direction[1] * 4.0,
                observer.position[2],
            )
            state.next_zombie_build_at = now + 2.0
            return self._intent(
                frame,
                now,
                movement=MovementIntent(
                    crouch=True,
                    affordance=MovementAffordance.BUILD_STEP,
                ),
                look=LookIntent(target, visible=False),
                tool_id=int(C.ZOMBIE_PREFAB_TOOL),
                action=BotAction(
                    BotActionKind.PLACE_PREFAB,
                    tool_id=int(C.ZOMBIE_PREFAB_TOOL),
                    position=target,
                    argument=str(zombie_prefab),
                    yaw=math.atan2(direction[1], direction[0]),
                ),
                priority=BotIntentPriority.TRAVERSAL,
                debug_role="zombie_build_climb",
            )
        if state.stuck_attempts >= 3:
            # A fresh query for the same strategic point can recreate the
            # identical failed crowd corridor. First leave it through a safe
            # local voxel edge, then let ordinary objective routing resume.
            return self._begin_route_escape(
                frame, observer, state, now, direction
            )
        return None

    def _active_route_escape(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
    ) -> BotIntent | None:
        """Continue a short safe detour selected after a failed corridor."""

        goal = state.route_escape_goal
        if goal is None:
            return None
        reached = math.hypot(
            float(goal[0]) - float(observer.position[0]),
            float(goal[1]) - float(observer.position[1]),
        ) <= 1.0
        progress_at = max(
            float(state.route_escape_started_at),
            float(state.last_progress_at),
        )
        escape_stalled = (
            state.route_escape_started_at > 0.0
            and now - progress_at >= _ROUTE_ESCAPE_STALL_SECONDS
        )
        if reached or escape_stalled:
            state.route_escape_goal = None
            state.route_escape_until = 0.0
            state.route_escape_started_at = 0.0
            if escape_stalled:
                state.stuck_attempts = max(3, state.stuck_attempts)
                state.next_stuck_recovery_at = 0.0
            return None
        if now >= state.route_escape_until:
            state.route_escape_goal = None
            state.route_escape_until = 0.0
            state.route_escape_started_at = 0.0
            return None
        planner = getattr(self.world, "action_planner", None)
        step = (
            planner.plan_local(
                observer.position,
                goal,
                abilities=self._movement_abilities(observer),
                topology_version=frame.topology_version,
                search_radius=16,
                max_expansions=2048,
            )
            if planner is not None
            else None
        )
        if step is None:
            state.route_escape_goal = None
            state.route_escape_until = 0.0
            state.route_escape_started_at = 0.0
            state.stuck_attempts = max(3, state.stuck_attempts)
            state.next_stuck_recovery_at = 0.0
            return None
        if not self._escape_step_has_braking_room(
            planner,
            observer.position,
            step,
        ):
            state.route_escape_goal = None
            state.route_escape_until = 0.0
            state.route_escape_started_at = 0.0
            state.stuck_attempts = max(3, state.stuck_attempts)
            state.next_stuck_recovery_at = 0.0
            return None
        state.last_path_direction = step.direction
        state.last_affordance = step.affordance
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                direction=step.direction,
                jump=step.affordance is MovementAffordance.JUMP,
                sprint=True,
                affordance=step.affordance,
            ),
            look=LookIntent(step.waypoint, visible=False),
            priority=BotIntentPriority.TRAVERSAL,
            debug_role="stuck_route_escape",
        )

    def _begin_route_escape(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
        failed_direction: Vector3,
    ) -> BotIntent | None:
        """Invalidate a failed crowd route and choose a bounded side detour."""

        failed_goal = state.path_goal
        reset_navigation = getattr(self.world, "reset_agent_navigation", None)
        if callable(reset_navigation):
            reset_navigation(observer.player_id)
        state.path.clear()
        state.path_goal = None
        state.path_topology_version = -1
        state.strategic_progress_at = now
        state.strategic_goal_distance = None
        state.regional_progress_anchor = observer.position
        state.regional_progress_at = now
        state.escape_build_cell = None
        state.escape_build_until = 0.0
        state.route_escape_failures += 1
        state.route_escape_started_at = 0.0

        planner = getattr(self.world, "action_planner", None)
        direct_step = (
            planner.plan_local(
                observer.position,
                failed_goal,
                abilities=self._movement_abilities(observer),
                topology_version=frame.topology_version,
                search_radius=24,
                max_expansions=4096,
            )
            if failed_goal is not None
            and state.route_escape_failures <= 2
            and planner is not None
            else None
        )
        if direct_step is not None and not self._escape_step_has_braking_room(
            planner,
            observer.position,
            direct_step,
        ):
            direct_step = None
        if direct_step is not None:
            # DetourCrowd can return a non-zero velocity into a dead-end face
            # when the real voxel exit begins with a short staircase/turn.
            # Temporarily follow the layered voxel route toward the original
            # strategic goal, recomputing its immediate edge each frame.
            state.route_escape_goal = failed_goal
            state.route_escape_until = now + 3.0
            state.route_escape_started_at = now
            state.stuck_attempts = 0
            state.last_path_direction = direct_step.direction
            state.last_affordance = direct_step.affordance
            return self._intent(
                frame,
                now,
                movement=MovementIntent(
                    direction=direct_step.direction,
                    jump=direct_step.affordance is MovementAffordance.JUMP,
                    sprint=True,
                    affordance=direct_step.affordance,
                ),
                look=LookIntent(direct_step.waypoint, visible=False),
                priority=BotIntentPriority.TRAVERSAL,
                debug_role="stuck_voxel_replan",
            )

        emergency_drop = getattr(self.world, "emergency_drop", None)
        drop = (
            emergency_drop(observer.position)
            if state.route_escape_failures >= 3
            and callable(emergency_drop)
            else None
        )
        if drop is not None:
            direction, landing = drop
            state.stuck_attempts = 0
            state.last_path_direction = direction
            state.last_affordance = MovementAffordance.DROP
            state.next_patrol_turn = 0.0
            return self._intent(
                frame,
                now,
                movement=MovementIntent(
                    direction=direction,
                    sprint=True,
                    affordance=MovementAffordance.DROP,
                ),
                look=LookIntent(landing, visible=False),
                priority=BotIntentPriority.SURVIVAL,
                debug_role="stuck_emergency_drop",
            )

        forward = _normalized_xy(failed_direction[0], failed_direction[1])
        if math.hypot(forward[0], forward[1]) <= 1e-6:
            forward = (
                math.cos(state.patrol_heading),
                math.sin(state.patrol_heading),
                0.0,
            )
        # Alternate sides on consecutive failures so an asymmetric doorway or
        # ledge cannot keep every bot probing the same blocked shoulder.
        side_sign = (
            1.0
            if (observer.player_id + state.route_escape_failures) % 2 == 0
            else -1.0
        )
        left = (-forward[1] * side_sign, forward[0] * side_sign, 0.0)
        candidates = (
            _normalized_xy(left[0] + forward[0] * 0.35, left[1] + forward[1] * 0.35),
            _normalized_xy(-left[0] + forward[0] * 0.20, -left[1] + forward[1] * 0.20),
            _normalized_xy(left[0] - forward[0] * 0.45, left[1] - forward[1] * 0.45),
            (-forward[0], -forward[1], 0.0),
        )
        distance = 6.0 + min(3, state.route_escape_failures) * 2.0
        terrain = getattr(planner, "terrain", None)
        immediate_probe = getattr(
            terrain, "direction_is_traversable", None
        )
        corridor_probe = getattr(
            terrain, "dry_corridor_is_traversable", None
        )
        for candidate in candidates:
            goal = (
                observer.position[0] + candidate[0] * distance,
                observer.position[1] + candidate[1] * distance,
                observer.position[2],
            )
            # A detour is allowed to move away from the objective briefly,
            # but never uses the outer map boundary as an escape corridor.
            if not (8.0 <= goal[0] < 504.0 and 8.0 <= goal[1] < 504.0):
                continue
            # Local A* is goal-biased and can begin every distant side-route
            # with the same bad shoreline edge. Before invoking it, accept a
            # short direct retreat only when both the immediate body probe and
            # a braking-width dry corridor prove that direction traversable.
            direct_retreat = (
                callable(immediate_probe)
                and callable(corridor_probe)
                and bool(
                    immediate_probe(
                        observer.position,
                        candidate,
                        MovementAffordance.WALK,
                    )
                )
                and bool(
                    corridor_probe(
                        observer.position,
                        candidate,
                        distance=1.5,
                    )
                )
            )
            if direct_retreat:
                retreat_distance = min(3.0, distance)
                retreat_goal = (
                    observer.position[0] + candidate[0] * retreat_distance,
                    observer.position[1] + candidate[1] * retreat_distance,
                    observer.position[2],
                )
                state.route_escape_goal = retreat_goal
                state.route_escape_until = now + 2.0
                state.route_escape_started_at = now
                state.stuck_attempts = 0
                state.last_path_direction = candidate
                state.last_affordance = MovementAffordance.WALK
                state.next_patrol_turn = 0.0
                return self._intent(
                    frame,
                    now,
                    movement=MovementIntent(
                        direction=candidate,
                        sprint=False,
                        affordance=MovementAffordance.WALK,
                    ),
                    look=LookIntent(retreat_goal, visible=False),
                    priority=BotIntentPriority.TRAVERSAL,
                    debug_role="stuck_route_retreat",
                )
            step = (
                planner.plan_local(
                    observer.position,
                    goal,
                    abilities=self._movement_abilities(observer),
                    topology_version=frame.topology_version,
                    search_radius=16,
                    max_expansions=2048,
                )
                if planner is not None
                else None
            )
            if step is None:
                continue
            if not self._escape_step_has_braking_room(
                planner,
                observer.position,
                step,
            ):
                continue
            state.route_escape_goal = goal
            state.route_escape_until = now + 2.0
            state.route_escape_started_at = now
            state.stuck_attempts = 0
            state.last_path_direction = step.direction
            state.last_affordance = step.affordance
            state.next_patrol_turn = 0.0
            return self._intent(
                frame,
                now,
                movement=MovementIntent(
                    direction=step.direction,
                    jump=step.affordance is MovementAffordance.JUMP,
                    sprint=True,
                    affordance=step.affordance,
                ),
                look=LookIntent(step.waypoint, visible=False),
                priority=BotIntentPriority.TRAVERSAL,
                debug_role="stuck_route_escape",
            )

        state.last_path_direction = (0.0, 0.0, 0.0)
        state.next_patrol_turn = 0.0
        # No alternate route exists. Keep the real progress clock stalled, but
        # recycle the bounded physical sequence so a durable wall can receive
        # more than three spade hits and a newly widened bridge can be retried.
        state.stuck_attempts = 0
        return None

    @staticmethod
    def _escape_step_has_braking_room(
        planner,
        position: Vector3,
        step,
    ) -> bool:
        """Keep a worker escape compatible with the live motor's dry brake."""
        if step.affordance not in {
            MovementAffordance.WALK,
            MovementAffordance.CROUCH,
        }:
            return True
        terrain = getattr(planner, "terrain", None)
        corridor = getattr(terrain, "dry_corridor_is_traversable", None)
        if not callable(corridor):
            return True
        return bool(
            corridor(
                position,
                step.direction,
                distance=1.5,
            )
        )

    def _local_objective_route(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        objective: Vector3,
    ) -> Vector3:
        """Recover an objective route when the global navmesh has no corridor.

        City maps frequently separate Recast polygons with destructible
        ledges, thin bridges, or authored vertical layers.  The bounded voxel
        action planner can still select the best safe immediate walk/jump/drop
        toward the goal.  If even that search has no exit, retain only the
        direct heading as recovery context while returning zero locomotion;
        ``_stuck_recovery`` can then breach, bridge, or place a class prefab
        without walking blindly over an edge or into water.
        """

        planner = getattr(self.world, "action_planner", None)
        step = (
            planner.plan_local(
                observer.position,
                objective,
                abilities=self._movement_abilities(observer),
                topology_version=frame.topology_version,
                search_radius=24,
                max_expansions=4096,
            )
            if planner is not None
            else None
        )
        if step is not None:
            state.last_path_direction = step.direction
            state.last_affordance = step.affordance
            state.path_goal = objective
            state.path_topology_version = int(frame.topology_version)
            return step.direction

        direct = _normalized_xy(
            objective[0] - observer.position[0],
            objective[1] - observer.position[1],
        )
        state.last_path_direction = direct
        state.last_affordance = MovementAffordance.WALK
        state.path_goal = objective
        state.path_topology_version = int(frame.topology_version)
        return 0.0, 0.0, 0.0

    def _class_world_action(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        profile: BotProfile,
        now: float,
        *,
        role: str = "",
    ) -> BotIntent | None:
        """Occasionally request one real class deployable while out of combat.

        This is intentionally conservative: the worker suggests only a tool
        present in the normalized life loadout, and the gameplay-thread service
        repeats class, held-tool, stock, range, and entity-limit checks.
        """

        defensive_role = any(
            token in str(role).lower()
            for token in ("defend", "guard", "fortify", "escort")
        )
        if state.next_world_action_at <= 0.0:
            state.next_world_action_at = now + self._rng.uniform(
                0.8 if defensive_role else 2.0,
                2.5 if defensive_role else 6.0,
            )
            return None
        if now < state.next_world_action_at:
            return None
        state.next_world_action_at = now + self._rng.uniform(
            6.0 if defensive_role else 18.0,
            14.0 if defensive_role else 35.0,
        )
        deployables = [
            tool
            for tool in observer.loadout
            if tool in _DEPLOYABLE_TOOLS
            and tool not in _GENERIC_DEPLOYABLE_EXCLUSIONS
        ]
        prefab_name = self._select_prefab(
            observer.prefabs,
            purpose="cover",
            selector=observer.player_id + frame.frame_id,
            block_budget=observer.blocks,
        )
        can_prefab = (
            defensive_role
            and int(C.PREFAB_TOOL) in observer.loadout
            and prefab_name is not None
        )
        if not deployables and not can_prefab:
            return None
        # Low-creativity profiles sometimes conserve equipment. The fixed RNG
        # seed keeps this behavior reproducible in soak and statistical tests.
        if (
            not defensive_role
            and self._rng.random() > 0.45 + 0.5 * float(profile.creativity)
        ):
            return None
        choose_prefab = can_prefab and (
            not deployables
            or self._rng.random()
            < (
                0.45 + 0.35 * float(profile.creativity)
                if defensive_role
                else 0.15 + 0.35 * float(profile.creativity)
            )
        )
        tool = (
            int(C.PREFAB_TOOL)
            if choose_prefab
            else int(deployables[observer.player_id % len(deployables)])
        )
        last_used = state.last_world_action.get(tool, -math.inf)
        if now - last_used < 15.0:
            return None
        if tool not in (int(C.DISGUISE_TOOL), int(C.PREFAB_TOOL)) and any(
            entity.alive
            and entity.owner_id == observer.player_id
            for entity in frame.entities
        ):
            return None

        forward = _normalized_xy(observer.orientation[0], observer.orientation[1])
        distance = 4.0 if choose_prefab else 1.5
        position = (
            observer.position[0] + forward[0] * distance,
            observer.position[1] + forward[1] * distance,
            observer.position[2] + 2.0,
        )
        if tool in {
            int(C.DYNAMITE_TOOL),
            int(C.LANDMINE_TOOL),
            int(C.C4_TOOL),
        } and not self._explosive_target_safe(
            frame,
            observer,
            position,
            tool,
            ignore_observer=True,
        ):
            return None
        state.last_world_action[tool] = now
        if choose_prefab:
            return self._intent(
                frame,
                now,
                movement=MovementIntent(
                    crouch=True, affordance=MovementAffordance.PLACE_PREFAB
                ),
                look=LookIntent(position, visible=False),
                tool_id=int(C.PREFAB_TOOL),
                action=BotAction(
                    BotActionKind.PLACE_PREFAB,
                    tool_id=int(C.PREFAB_TOOL),
                    position=position,
                    argument=str(prefab_name),
                    yaw=math.atan2(observer.orientation[1], observer.orientation[0]),
                ),
            )
        action = BotAction(
            BotActionKind.DEPLOY,
            tool_id=tool,
            position=None if tool == int(C.DISGUISE_TOOL) else position,
            face=4,
            yaw=math.atan2(observer.orientation[1], observer.orientation[0]),
        )
        return self._intent(
            frame,
            now,
            movement=MovementIntent(),
            look=LookIntent(position, visible=False),
            tool_id=tool,
            action=action,
        )

    @staticmethod
    def _objective_decision(
        frame: PerceptionFrame, observer: PlayerSnapshot
    ) -> ModeBotDecision | None:
        """Return legal mode/phase knowledge and its inspectable role."""
        return objective_decision_for(frame, observer)

    @staticmethod
    def _resource_goal(
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        *,
        state: _BrainState | None = None,
        now: float | None = None,
    ) -> Vector3 | None:
        """Choose a critical resource, abandoning one pickup after 3 seconds.

        The abandonment is per bot and per entity-position pair.  Other bots
        can still collect the pickup, and a terrain-settled pickup receives a
        fresh key at its new position.  Calls without state retain the pure
        nearest-resource behavior used by characterization tests/tools.
        """

        desired_types: set[int] = set()
        if observer.health < 50:
            desired_types.add(int(C.HEALTH_CRATE))
        if observer.ammo_clip <= 1 and observer.ammo_reserve <= 12:
            desired_types.add(int(C.AMMO_CRATE))
        if observer.blocks < 12 and int(C.BLOCK_TOOL) in observer.loadout:
            desired_types.add(int(C.BLOCK_CRATE))
        if not desired_types:
            if state is not None:
                state.resource_target = None
                state.resource_target_since = 0.0
            return None
        candidates = [
            entity
            for entity in frame.entities
            if entity.alive
            and entity.entity_type in desired_types
            and entity.team in (-1, int(C.TEAM_NEUTRAL), observer.team)
            and _distance_squared(observer.position, entity.position) <= 160.0**2
            and (
                state is None
                or (int(entity.entity_id), entity.position)
                not in state.ignored_resources
            )
        ]
        if not candidates:
            if state is not None:
                state.resource_target = None
                state.resource_target_since = 0.0
            return None
        selected = min(
            candidates,
            key=lambda entity: _distance_squared(observer.position, entity.position),
        )
        if state is None or now is None:
            return selected.position

        key = (int(selected.entity_id), selected.position)
        if state.resource_target != key:
            state.resource_target = key
            state.resource_target_since = float(now)
            return selected.position
        if float(now) - float(state.resource_target_since) < 3.0:
            return selected.position

        # A moving/reused pickup cannot grow one bot's rejection memory for
        # an entire server lifetime. Life changes clear this set; the cap is
        # a final bound for pathological entity churn within one life.
        if len(state.ignored_resources) >= 128:
            state.ignored_resources.clear()
        state.ignored_resources.add(key)
        state.resource_target = None
        state.resource_target_since = 0.0
        # Try the next reachable resource immediately rather than idling for
        # another behavior-tree period at the rejected crate.
        remaining = [
            entity for entity in candidates
            if (int(entity.entity_id), entity.position) != key
        ]
        if not remaining:
            return None
        selected = min(
            remaining,
            key=lambda entity: _distance_squared(
                observer.position, entity.position
            ),
        )
        state.resource_target = (int(selected.entity_id), selected.position)
        state.resource_target_since = float(now)
        return selected.position

    def _engage(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        target: PlayerSnapshot,
        state: _BrainState,
        profile: BotProfile,
        now: float,
    ) -> BotIntent:
        if (
            int(observer.class_id) in _ZOMBIE_CLASSES
            and int(C.ZOMBIEHAND_TOOL) in observer.loadout
        ):
            return self._engage_zombie(
                frame,
                observer,
                target,
                state,
                now,
            )

        dx = target.position[0] - observer.position[0]
        dy = target.position[1] - observer.position[1]
        distance = math.hypot(dx, dy)
        immediate_threat = distance <= 8.0
        weapon_tool = (
            int(observer.weapon_tool)
            if int(observer.weapon_tool) in observer.loadout
            else int(observer.tool)
        )
        melee_tool = self._preferred_melee_tool(observer)
        dry_weapon = observer.ammo_clip <= 0 and observer.ammo_reserve <= 0
        # The weapon's engagement envelope decides spacing: snipers hold far
        # and stationary, shotguns and SMGs close in, rifles fight midrange.
        envelope = envelope_for(weapon_tool)
        band_min = float(envelope.ideal_min)
        band_max = float(envelope.ideal_max)
        stationary_hold = False
        cover_direction = (0.0, 0.0, 0.0)
        cover_reader = getattr(self.world, "cover_direction", None)
        if (
            callable(cover_reader)
            and (
                observer.health < 40
                or (profile.caution > 0.75 and distance < band_min)
            )
        ):
            cover_direction = cover_reader(observer.position, target.eye)
        seeking_cover = math.hypot(cover_direction[0], cover_direction[1]) > 0.1
        if dry_weapon and melee_tool is not None:
            # Once ammunition is truly exhausted, close for a real selected
            # melee attack instead of dry-firing the primary forever.
            movement = self._path_direction(
                observer.position,
                target.position,
                state,
                now,
                agent_id=observer.player_id,
                velocity=observer.velocity,
                abilities=self._movement_abilities(observer),
            )
            seeking_cover = False
        elif seeking_cover:
            # Peek discipline: settle behind cover for a beat, lean out for a
            # short strafe, then tuck back in, instead of oscillating.
            state.last_affordance = MovementAffordance.WALK
            if now >= state.hold_until and now >= state.peek_until:
                if state.peek_until >= state.hold_until:
                    # A peek (or a fresh engagement) just ended: settle in.
                    state.hold_until = now + self._rng.uniform(1.2, 2.5)
                else:
                    # The hold just ended: lean out.
                    state.peek_until = now + self._rng.uniform(0.6, 1.2)
                    state.strafe_sign = (
                        -1.0 if self._rng.random() < 0.5 else 1.0
                    )
            if now < state.hold_until:
                movement = cover_direction
            else:
                movement = _normalized_xy(
                    -dy * state.strafe_sign,
                    dx * state.strafe_sign,
                )
        elif distance > band_max:
            movement = self._path_direction(
                observer.position,
                target.position,
                state,
                now,
                agent_id=observer.player_id,
                velocity=observer.velocity,
                abilities=self._movement_abilities(observer),
            )
        elif distance < max(2.0, band_min * 0.6):
            state.last_affordance = MovementAffordance.WALK
            movement = _normalized_xy(-dx, -dy)
        elif envelope.prefers_stationary and distance >= band_min:
            # Hold a firing position; shift a few blocks laterally every few
            # seconds so the bot is not a permanent statue.
            state.last_affordance = MovementAffordance.WALK
            if now >= state.next_reposition_at:
                state.strafe_sign = -1.0 if self._rng.random() < 0.5 else 1.0
                state.reposition_until = now + self._rng.uniform(0.8, 1.5)
                state.next_reposition_at = now + self._rng.uniform(4.0, 7.0)
            if now < state.reposition_until:
                movement = _normalized_xy(
                    -dy * state.strafe_sign,
                    dx * state.strafe_sign,
                )
            else:
                stationary_hold = True
                movement = (0.0, 0.0, 0.0)
        else:
            state.last_affordance = MovementAffordance.WALK
            # Hold a strafe for a human-sized interval. Random-per-frame side
            # changes look like jitter and make the bot easier, not smarter.
            if now >= state.next_strafe_switch_at:
                state.strafe_sign = -1.0 if self._rng.random() < 0.5 else 1.0
                state.next_strafe_switch_at = now + self._rng.uniform(0.7, 1.6)
            movement = _normalized_xy(
                -dy * state.strafe_sign,
                dx * state.strafe_sign,
            )
        action = BotAction()
        oriented_stock = dict(observer.oriented_stock)
        oriented: list[tuple[float, int]] = []
        for tool in observer.loadout:
            normalized_tool = int(tool)
            if (
                normalized_tool not in _ORIENTED_TOOLS
                or oriented_stock.get(normalized_tool, 1) <= 0
            ):
                continue
            utility = self._oriented_attack_utility(
                frame, observer, target, normalized_tool
            )
            if utility > 0.0:
                oriented.append((utility, normalized_tool))
        oriented.sort()
        if observer.reloading:
            # Tool selection outside the primary weapon cancels Player.reload;
            # wait for the already-started reload instead of selecting a
            # grenade/deployable on the next perception frame.
            action = BotAction()
        elif observer.ammo_clip <= 0 and observer.ammo_reserve > 0:
            action = BotAction(BotActionKind.RELOAD, tool_id=weapon_tool)
        elif dry_weapon:
            if melee_tool is not None and distance <= 4.25:
                weapon_tool = melee_tool
                action = BotAction(BotActionKind.MELEE, tool_id=melee_tool)
        elif seeking_cover and observer.health < 30 and not immediate_threat:
            action = BotAction()
        else:
            cover = self._combat_cover_intent(
                frame,
                observer,
                target,
                state,
                profile,
                now,
            )
            if cover is not None:
                return cover

        if (
            action.kind is BotActionKind.NONE
            and not dry_weapon
            and not observer.reloading
            and not (
                seeking_cover
                and observer.health < 30
                and not immediate_threat
            )
            and now - state.acquired_at
            >= profile.reaction_time + state.reaction_bonus
            and (
                oriented
                and 12.0 <= distance <= 65.0
                and now >= state.next_oriented_at
                and now
                >= self._team_oriented_ready_at.get(
                    int(observer.team), -math.inf
                )
                and self._rng.random()
                < 0.10 + float(profile.creativity) * 0.22
            )
        ):
            _utility, chosen = oriented[-1]
            state.next_oriented_at = now + self._rng.uniform(5.0, 11.0)
            self._team_oriented_ready_at[int(observer.team)] = (
                now + _TEAM_ORIENTED_SPACING_SECONDS
            )
            action = BotAction(BotActionKind.ORIENTED, tool_id=chosen)
            weapon_tool = chosen
        elif (
            action.kind is BotActionKind.NONE
            and not dry_weapon
            and not observer.reloading
            and not (
                seeking_cover
                and observer.health < 30
                and not immediate_threat
            )
            and observer.ammo_clip > 0
            and distance <= float(envelope.hard_max)
            and now - state.acquired_at
            >= profile.reaction_time + state.reaction_bonus
        ):
            burst_low, burst_high = envelope.burst_shots
            action = BotAction(
                BotActionKind.FIRE,
                tool_id=weapon_tool,
                burst=self._rng.randint(int(burst_low), int(burst_high)),
                # Disciplined shooters pause less between bursts.
                burst_pause=self._rng.uniform(*envelope.burst_pause)
                * (1.3 - 0.5 * float(profile.burst_discipline)),
            )

        jump = self._combat_jump_due(
            observer,
            state,
            profile,
            now,
            distance=distance,
            seeking_cover=seeking_cover,
        )
        # AoS z increases downward. The torso sample is therefore below the
        # eye; an occasional persistent head decision uses the eye sample.
        lead_time = min(0.18, max(0.0, float(profile.tracking_delay)))
        aim_offset_z = 0.0 if state.aim_head else 1.15
        aim_target = (
            target.eye[0] + state.delayed_target_velocity[0] * lead_time,
            target.eye[1] + state.delayed_target_velocity[1] * lead_time,
            target.eye[2]
            + state.delayed_target_velocity[2] * lead_time
            + aim_offset_z,
        )
        scoped = (
            int(weapon_tool) in _SNIPER_TOOLS
            and distance >= 18.0
            and not dry_weapon
        )
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                direction=movement,
                jump=jump,
                crouch=(seeking_cover and observer.health < 35)
                or stationary_hold,
                sneak=seeking_cover and observer.health < 55,
                sprint=(distance > band_max)
                or (dry_weapon and melee_tool is not None),
                affordance=state.last_affordance,
            ),
            # The frozen aim point stays the fallback; the ids authorize the
            # director's short-lease live refinement of this visible target.
            look=LookIntent(
                aim_target,
                visible=True,
                target_player_id=int(target.player_id),
                target_generation=int(target.generation),
                aim_offset_z=aim_offset_z,
            ),
            tool_id=weapon_tool,
            action=action,
            priority=BotIntentPriority.COMBAT,
            secondary_fire=scoped,
            zoom=scoped,
            debug_role=("marksman_scoped_engage" if scoped else "combat_engage"),
        )

    def _engage_zombie(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        target: PlayerSnapshot,
        state: _BrainState,
        now: float,
    ) -> BotIntent:
        """Commit an infected bot to contact-range pursuit and claw attacks.

        Zombie hands are melee weapons even when the compatibility snapshot
        contains non-zero ammo counters.  They never apply firearm preferred
        range, cover retreat, reload, or reaction-delay branches.  Terrain
        pathing remains worker-owned, while the final hand swing still passes
        through authoritative LOS, cadence, hitbox, damage, and replication.
        """

        dx = target.position[0] - observer.position[0]
        dy = target.position[1] - observer.position[1]
        distance = math.hypot(dx, dy)
        direct = _normalized_xy(dx, dy)
        local_step = None
        action_planner = getattr(self.world, "action_planner", None)
        if distance <= 24.0 and action_planner is not None:
            local_step = action_planner.plan_local(
                observer.position,
                target.position,
                abilities=self._movement_abilities(observer),
                topology_version=frame.topology_version,
            )
        if local_step is not None:
            movement = local_step.direction
            state.last_path_direction = movement
            state.last_affordance = local_step.affordance
            state.path_goal = target.position
            state.path_topology_version = int(frame.topology_version)
        elif distance <= 6.0:
            # Detour correctly ends at the nearest walkable polygon, but a
            # same-height living target's occupied cell is not itself a path
            # endpoint. Direct steering is only the fail-safe after the local
            # height-aware planner finds no topology action.
            movement = direct
            state.last_path_direction = movement
            state.last_affordance = MovementAffordance.WALK
        else:
            movement = self._path_direction(
                observer.position,
                target.position,
                state,
                now,
                agent_id=observer.player_id,
                velocity=observer.velocity,
                abilities=self._movement_abilities(observer),
            )
        if math.hypot(movement[0], movement[1]) <= 0.1 and distance > 6.0:
            # Preserve a safe zero motor request while giving the next stalled
            # frame a meaningful breach/build heading.  Never substitute the
            # direct vector as locomotion here: it may cross water or an edge.
            state.last_path_direction = direct
            state.last_affordance = MovementAffordance.WALK
            state.path_goal = target.position
            state.path_topology_version = int(frame.topology_version)

        class_id = int(observer.class_id)
        if class_id == int(C.CLASS_FAST_ZOMBIE) and 4.0 < distance < 24.0:
            # A small persistent weave makes the fast variant harder to track
            # without overriding the collision-safe forward path.
            if now >= state.next_strafe_switch_at:
                state.strafe_sign *= -1.0
                state.next_strafe_switch_at = now + self._rng.uniform(0.55, 0.9)
            movement = _normalized_xy(
                movement[0] - movement[1] * 0.18 * state.strafe_sign,
                movement[1] + movement[0] * 0.18 * state.strafe_sign,
            )

        aim_target = (
            target.eye[0],
            target.eye[1],
            target.eye[2] + 1.0,
        )
        aim_delta = tuple(
            aim_target[index] - observer.eye[index] for index in range(3)
        )
        aim_length = math.sqrt(sum(value * value for value in aim_delta))
        orientation_length = math.sqrt(
            sum(float(value) * float(value) for value in observer.orientation)
        )
        alignment = -1.0
        if aim_length > 1e-6 and orientation_length > 1e-6:
            alignment = sum(
                float(observer.orientation[index]) * aim_delta[index]
                for index in range(3)
            ) / (aim_length * orientation_length)

        # Alignment matters at arm's length but not in a contact scrum: the
        # angle to a torso point swings wildly while bodies collide, so the
        # gate tapers away at point-blank range (mirrored by the director).
        if distance <= 1.2:
            min_alignment = -1.0
        elif distance <= 2.5:
            min_alignment = 0.72 * ((distance - 1.2) / 1.3)
        else:
            min_alignment = 0.72
        action = BotAction()
        if distance <= 4.25 and alignment >= min_alignment:
            action = BotAction(
                BotActionKind.MELEE,
                tool_id=int(C.ZOMBIEHAND_TOOL),
            )

        jump = state.last_affordance is MovementAffordance.JUMP or (
            self._zombie_jump_due(
                observer,
                target,
                state,
                now,
                distance=distance,
            )
        )
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                direction=movement,
                jump=jump,
                sprint=(
                    state.last_affordance is MovementAffordance.WALK
                    and distance > 3.0
                ),
                affordance=state.last_affordance,
            ),
            look=LookIntent(
                aim_target,
                visible=True,
                target_player_id=int(target.player_id),
                target_generation=int(target.generation),
                aim_offset_z=1.0,
            ),
            tool_id=int(C.ZOMBIEHAND_TOOL),
            action=action,
            priority=BotIntentPriority.COMBAT,
            debug_role=(
                "zombie_contact_strike"
                if action.kind is BotActionKind.MELEE
                else "zombie_contact_charge"
            ),
        )

    def _zombie_jump_due(
        self,
        observer: PlayerSnapshot,
        target: PlayerSnapshot,
        state: _BrainState,
        now: float,
        *,
        distance: float,
    ) -> bool:
        """Apply internal Fast/Jump Zombie mobility without jump spamming."""

        if not observer.grounded or now < state.next_combat_jump_at:
            return False
        class_id = int(observer.class_id)
        target_is_higher = target.position[2] < observer.position[2] - 0.75
        if class_id == int(C.CLASS_JUMP_ZOMBIE):
            if not (target_is_higher or 2.0 <= distance <= 38.0):
                return False
            state.next_combat_jump_at = now + self._rng.uniform(0.65, 0.9)
            return True
        if class_id == int(C.CLASS_FAST_ZOMBIE):
            if not (target_is_higher or 5.0 <= distance <= 28.0):
                return False
            state.next_combat_jump_at = now + self._rng.uniform(1.15, 1.7)
            return target_is_higher or self._rng.random() < 0.45
        if class_id == int(C.CLASS_ZOMBIE) and target_is_higher and distance <= 18.0:
            state.next_combat_jump_at = now + self._rng.uniform(1.4, 2.0)
            return True
        return False

    def _combat_cover_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        target: PlayerSnapshot,
        state: _BrainState,
        profile: BotProfile,
        now: float,
    ) -> BotIntent | None:
        """Build replicated line or prefab cover under serious pressure."""

        if _distance_squared(observer.position, target.position) <= 8.0 ** 2:
            return None
        pressured = observer.health <= 35 or (
            observer.health <= 55 and profile.caution >= 0.75
        )
        if not pressured or now < state.next_cover_build_at:
            return None
        line_reader = getattr(self.world, "cover_build_line", None)
        line = (
            line_reader(observer.position, target.eye)
            if callable(line_reader)
            else None
        )
        position_reader = getattr(self.world, "cover_build_cell", None)
        cell = None
        if line is None and callable(position_reader):
            cell = position_reader(observer.position, target.eye)
        if line is None and cell is None:
            state.next_cover_build_at = now + 1.0
            return None
        if line is not None:
            start, end = line
            block_cost = max(
                abs(int(end[index]) - int(start[index])) for index in range(3)
            ) + 1
            cover_position = tuple(
                (float(start[index]) + float(end[index])) * 0.5
                for index in range(3)
            )
        else:
            start = end = None
            block_cost = 1
            cover_position = tuple(float(value) for value in cell)
        can_block = (
            int(C.BLOCK_TOOL) in observer.loadout
            and observer.blocks >= block_cost
        )
        prefab_name = self._select_prefab(
            observer.prefabs,
            purpose="cover",
            selector=observer.player_id + frame.frame_id,
            block_budget=observer.blocks,
        )
        can_prefab = (
            int(C.PREFAB_TOOL) in observer.loadout
            and prefab_name is not None
        )
        if not can_block and not can_prefab:
            return None

        use_prefab = can_prefab and (
            not can_block
            or (
                observer.health <= 22
                and self._rng.random() < 0.35 + profile.creativity * 0.45
            )
        )
        position = cover_position
        yaw = math.atan2(
            target.position[1] - observer.position[1],
            target.position[0] - observer.position[0],
        )
        if use_prefab:
            state.next_cover_build_at = now + self._rng.uniform(6.0, 9.0)
            action = BotAction(
                BotActionKind.PLACE_PREFAB,
                tool_id=int(C.PREFAB_TOOL),
                position=position,
                argument=str(prefab_name),
                yaw=yaw,
            )
            tool = int(C.PREFAB_TOOL)
            role = "combat_prefab_cover"
        elif line is not None:
            # A short cadence permits a second supported row after the first
            # line commits, while construction reservations prevent overlap.
            state.next_cover_build_at = now + self._rng.uniform(1.1, 1.8)
            action = BotAction(
                BotActionKind.BUILD_LINE,
                tool_id=int(C.BLOCK_TOOL),
                position=tuple(float(value) for value in start),
                end_position=tuple(float(value) for value in end),
            )
            tool = int(C.BLOCK_TOOL)
            role = "combat_block_line_cover"
        else:
            state.next_cover_build_at = now + self._rng.uniform(1.1, 1.8)
            action = BotAction(
                BotActionKind.BUILD,
                tool_id=int(C.BLOCK_TOOL),
                position=position,
            )
            tool = int(C.BLOCK_TOOL)
            role = "combat_block_cover"
        state.last_world_action[tool] = now
        return self._intent(
            frame,
            now,
            movement=MovementIntent(
                crouch=True,
                sneak=True,
                affordance=MovementAffordance.BUILD_STEP,
            ),
            look=LookIntent(position, visible=False),
            tool_id=tool,
            action=action,
            priority=BotIntentPriority.COMBAT,
            debug_role=role,
        )

    def _combat_jump_due(
        self,
        observer: PlayerSnapshot,
        state: _BrainState,
        profile: BotProfile,
        now: float,
        *,
        distance: float,
        seeking_cover: bool,
    ) -> bool:
        """Return one grounded, cooldown-bounded evasive jump decision."""

        if (
            not observer.grounded
            or observer.reloading
            or seeking_cover
            or not 5.0 <= distance <= 45.0
            or now < state.next_combat_jump_at
        ):
            return False
        state.next_combat_jump_at = now + max(
            1.1,
            self._rng.uniform(2.0, 3.2) - profile.skill * 0.8,
        )
        chance = 0.10 + profile.skill * 0.32 + profile.aggression * 0.18
        return (
            profile.skill + profile.aggression >= 1.60
            or self._rng.random() < chance
        )

    def _path_direction(
        self,
        start: Vector3,
        goal: Vector3,
        state: _BrainState,
        now: float,
        *,
        agent_id: int,
        velocity: Vector3,
        abilities: frozenset[MovementAffordance],
    ) -> Vector3:
        # Retain the strategic goal for progress projection. Previously only
        # fallback voxel routes populated this field, so successful native
        # Recast routes could not measure whether they approached their goal.
        previous_goal = state.path_goal
        topology_version = int(getattr(self.world, "topology_version", -1))
        route_changed = (
            previous_goal is None
            or math.hypot(
                float(previous_goal[0]) - float(goal[0]),
                float(previous_goal[1]) - float(goal[1]),
            )
            >= 6.0
            or state.path_topology_version != topology_version
        )
        if previous_goal is None:
            state.regional_progress_anchor = start
            state.regional_progress_at = now
        if (
            route_changed
            or state.strategic_progress_at <= 0.0
        ):
            state.strategic_progress_at = now
            state.strategic_goal_distance = math.hypot(
                float(goal[0]) - float(start[0]),
                float(goal[1]) - float(start[1]),
            )
        state.path_goal = goal
        state.path_topology_version = topology_version
        elapsed = max(0.0, now - self._path_refill_at)
        self._path_refill_at = now
        self._path_tokens = min(
            self._path_rate,
            self._path_tokens + elapsed * self._path_rate,
        )
        active_bots = max(1, len(self._states))
        request_interval = active_bots / self._path_rate
        if (
            not route_changed
            and now + 1e-9 < state.next_path_request_at
        ):
            return state.last_path_direction
        if self._path_tokens < 1.0:
            return state.last_path_direction
        self._path_tokens -= 1.0
        state.next_path_request_at = now + request_interval
        state.last_path_direction = self.world.next_path_direction(
            start,
            goal,
            agent_id=agent_id,
            velocity=velocity,
            abilities=abilities,
        )
        affordance_reader = getattr(self.world, "last_affordance", None)
        state.last_affordance = (
            affordance_reader(agent_id)
            if callable(affordance_reader)
            else MovementAffordance.WALK
        )
        return state.last_path_direction

    @staticmethod
    def _movement_abilities(
        observer: PlayerSnapshot,
    ) -> frozenset[MovementAffordance]:
        """Derive legal topology edges from the normalized current life."""

        abilities = {
            MovementAffordance.CROUCH,
            MovementAffordance.JUMP,
            MovementAffordance.DROP,
        }
        if (
            int(observer.jetpack_id)
            in {
                int(C.JETPACK_NORMAL),
                int(C.JETPACK2),
                int(C.JETPACK_ENGINEER),
                int(C.JETPACK_UGCBUILDER),
            }
            and float(observer.jetpack_fuel) >= 10.0
        ):
            abilities.add(MovementAffordance.JETPACK)
        return frozenset(abilities)

    def _patrol(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BrainState,
        now: float,
    ) -> BotIntent:
        state.last_affordance = MovementAffordance.WALK
        if now >= state.next_patrol_turn:
            state.patrol_heading = self._rng.uniform(-math.pi, math.pi)
            state.next_patrol_turn = now + self._rng.uniform(1.5, 3.5)
        direction = (
            math.cos(state.patrol_heading),
            math.sin(state.patrol_heading),
            0.0,
        )
        # Sweep the gaze slowly across the heading instead of a fixed stare:
        # patrolling humans keep checking their flanks.
        scan = state.patrol_heading + 0.7 * math.sin(
            now * 0.9 + float(observer.player_id)
        )
        look_target = (
            observer.eye[0] + math.cos(scan) * 8.0,
            observer.eye[1] + math.sin(scan) * 8.0,
            observer.eye[2],
        )
        return self._intent(
            frame,
            now,
            movement=MovementIntent(direction=direction),
            look=LookIntent(look_target, visible=False),
        )

    def _visible_enemies(
        self,
        observer: PlayerSnapshot,
        players: tuple[PlayerSnapshot, ...],
        state: _BrainState,
    ) -> list[PlayerSnapshot]:
        result: list[PlayerSnapshot] = []
        alerted = bool(state.contacts)
        fov_cos = _ALERTED_FOV_COS if alerted else _UNALERTED_FOV_COS
        forward_xy = _normalized_xy(observer.orientation[0], observer.orientation[1])
        for candidate in players:
            if (
                candidate.player_id == observer.player_id
                or candidate.team == observer.team
                or not candidate.alive
                or not candidate.spawned
            ):
                continue
            delta = (
                candidate.eye[0] - observer.eye[0],
                candidate.eye[1] - observer.eye[1],
                candidate.eye[2] - observer.eye[2],
            )
            distance_sq = sum(value * value for value in delta)
            if distance_sq > _VISUAL_RANGE * _VISUAL_RANGE:
                continue
            planar = math.hypot(delta[0], delta[1])
            if planar > 1e-6:
                facing_dot = (
                    forward_xy[0] * delta[0] + forward_xy[1] * delta[1]
                ) / planar
                if facing_dot < fov_cos:
                    continue
            if self.world.has_line_of_sight(observer.eye, candidate.eye):
                result.append(candidate)
        return result

    @staticmethod
    def _expire_contacts(state: _BrainState, now: float) -> None:
        expired = [
            player_id
            for player_id, contact in state.contacts.items()
            if contact.age(now) > _CONTACT_LIFETIME
        ]
        for player_id in expired:
            del state.contacts[player_id]
            if state.target_id == player_id:
                state.target_id = None

    @staticmethod
    def _best_contact(
        observer: PlayerSnapshot, state: _BrainState, now: float
    ) -> LastSeenContact | None:
        if not state.contacts:
            return None
        for contact in state.contacts.values():
            contact.uncertainty = min(24.0, contact.age(now) * 3.0)
        return min(
            state.contacts.values(),
            key=lambda contact: _distance_squared(observer.position, contact.position),
        )

    @staticmethod
    def _intent(
        frame: PerceptionFrame,
        now: float,
        *,
        movement: MovementIntent,
        look: LookIntent | None,
        tool_id: int = -1,
        action: BotAction = BotAction(),
        priority: BotIntentPriority = BotIntentPriority.ROUTINE,
        secondary_fire: bool = False,
        zoom: bool = False,
        debug_role: str = "",
    ) -> BotIntent:
        direction = movement.direction
        debug_path = ()
        observer = next(
            (
                player
                for player in frame.players
                if player.player_id == frame.observer_id
            ),
            None,
        )
        resolved_tool = int(tool_id)
        if (
            resolved_tool < 0
            and action.kind is BotActionKind.NONE
            and observer is not None
            and int(observer.weapon_tool) in observer.loadout
        ):
            # Utility actions select their held item authoritatively. The next
            # passive/navigation intent must draw the life primary again or an
            # engineer can retain a prefab, block, or pickaxe indefinitely.
            resolved_tool = int(observer.weapon_tool)
        if math.hypot(direction[0], direction[1]) > 1e-6:
            if observer is not None:
                debug_path = (
                    observer.position,
                    (
                        observer.position[0] + direction[0] * 4.0,
                        observer.position[1] + direction[1] * 4.0,
                        observer.position[2] + direction[2] * 4.0,
                    ),
                )
        emitted_at = max(time.monotonic(), float(frame.created_at), float(now))
        return BotIntent(
            bot_id=frame.observer_id,
            bot_generation=frame.observer_generation,
            frame_id=frame.frame_id,
            map_epoch=frame.map_epoch,
            mode_epoch=frame.mode_epoch,
            topology_version=frame.topology_version,
            created_at=emitted_at,
            expires_at=emitted_at + _INTENT_TTL_SECONDS,
            movement=movement,
            look=look,
            tool_id=resolved_tool,
            action=action,
            priority=priority,
            secondary_fire=bool(secondary_fire),
            zoom=bool(zoom),
            debug_goal=look.target if look is not None else None,
            debug_path=debug_path,
            debug_role=str(debug_role),
        )


def _fallback_profile(player_id: int) -> BotProfile:
    return BotProfile(
        name=f"Bot{player_id}",
        difficulty="normal",
        skill=0.55,
        aggression=0.55,
        caution=0.50,
        teamwork=0.55,
        creativity=0.50,
        reaction_time=0.32,
        tracking_delay=0.12,
        turn_speed=3.8,
        turn_acceleration=13.0,
        recoil_control=0.60,
        burst_discipline=0.60,
        preferred_range=24.0,
        aim_noise=0.055,
    )


def _process_worker_batch(
    world: WorkerVoxelWorld,
    brain: BotBrain,
    messages: Iterable[object],
) -> tuple[bool, list[BotIntent]]:
    """Apply world messages and decide only the newest frame per bot life."""

    frames: dict[tuple[int, int], PerceptionFrame] = {}
    for message in messages:
        if isinstance(message, WorkerShutdown):
            return True, []
        if isinstance(message, MapSnapshot):
            world.load(message)
            brain.reset_for_map(message.map_epoch)
            # Any earlier frame describes the old map generation.
            frames.clear()
            continue
        if isinstance(message, WorldDelta):
            world.apply(message)
            continue
        if isinstance(message, PerceptionFrame):
            key = int(message.observer_id), int(message.observer_generation)
            previous = frames.get(key)
            if previous is None or message.frame_id > previous.frame_id:
                frames[key] = message

    intents: list[BotIntent] = []
    for frame in sorted(frames.values(), key=lambda item: item.frame_id):
        # The director also checks exact epochs. Filtering here avoids doing
        # expensive LOS/path work for an intention guaranteed to be rejected.
        if (
            frame.map_epoch != world.map_epoch
            or frame.topology_version != world.topology_version
        ):
            continue
        intent = brain.decide(frame)
        if intent is not None:
            intents.append(intent)
    return False, intents


def run_worker(
    input_queue,
    output_queue,
    seed: int = 0,
    decision_hz: float = 8.0,
    path_requests_per_second: float = 24.0,
) -> None:
    """Child-process entry point; process messages until orderly shutdown."""

    world = WorkerVoxelWorld()
    brain = BotBrain(
        world,
        seed=seed,
        decision_hz=decision_hz,
        path_requests_per_second=path_requests_per_second,
    )
    snapshot_assembler = MapSnapshotAssembler()
    batch_id = 0
    while True:
        try:
            first = input_queue.get(timeout=0.25)
        except queue.Empty:
            world.tactical.rebuild(64)
            continue
        messages = [first]
        # Multiprocessing queues are intentionally allowed to contain a short
        # burst. Drain it now and coalesce snapshots before doing any costly
        # path/LOS work, preventing old frames from accumulating over a match.
        for _ in range(63):
            try:
                messages.append(input_queue.get_nowait())
            except queue.Empty:
                break
        try:
            decoded_messages = snapshot_assembler.consume(messages)
        except SnapshotTransportError:
            # The parent will observe this non-zero child exit, discard its
            # partial queue, and resend the latest canonical map to a clean
            # assembler. Continuing with an old collision world is unsafe.
            logger.exception("AI worker rejected map snapshot transport")
            raise
        processed_frame_id = max(
            (
                int(message.frame_id)
                for message in decoded_messages
                if isinstance(message, PerceptionFrame)
            ),
            default=-1,
        )
        world.begin_batch()
        shutdown, intents = _process_worker_batch(
            world,
            brain,
            decoded_messages,
        )
        if shutdown:
            return
        # Between batches, advance the incremental tactical layer a bounded
        # step so a full map summarizes within a couple of seconds without
        # ever competing with perception or pathfinding work.
        world.tactical.rebuild(64)
        batch_id += 1
        heartbeat = WorkerHeartbeat(
            batch_id=batch_id,
            processed_frame_id=processed_frame_id,
            map_epoch=int(world.map_epoch),
            topology_version=int(world.topology_version),
            snapshot_transfer_id=(
                snapshot_assembler.last_completed_transfer_id
            ),
        )
        try:
            # Control acknowledgements precede lossy intentions.  A healthy
            # zero-intent state (countdown, cadence skip, stale frame) must
            # still renew the supervisor's watchdog lease.
            output_queue.put(heartbeat, timeout=0.05)
        except queue.Full:
            # The bridge drains this bounded queue every 5 ms. If its process
            # is briefly descheduled, the next batch acknowledgement catches
            # up without allowing stale gameplay intentions to accumulate.
            pass
        for intent in intents:
            try:
                output_queue.put_nowait(intent)
            except queue.Full:
                # Results are intentionally lossy: an expired movement
                # suggestion is less useful than a newer pending frame.
                break
