"""Gameplay-thread bot ownership, lifecycle, intent validation, and motors."""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from heapq import nsmallest
from typing import TYPE_CHECKING

import shared.constants as C
from shared.packet import PlayerLeft

from server.class_selection import normalize_class_selection
from server.game_constants import (
    DEFAULT_WEAPON_TOOL,
    SPADE_TOOL_IDS,
    TEAM1,
    TEAM2,
    WEAPON_PROFILES,
)

from .combat_profiles import recoil_kick_for
from .gateway import BotActionGateway
from .messages import (
    BotAction,
    BotActionKind,
    BotIntent,
    BotIntentPriority,
    BotProfile,
    EntitySnapshot,
    MapSnapshot,
    MovementAffordance,
    ObjectiveSnapshot,
    PerceptionFrame,
    PlayerSnapshot,
    VoxelChange,
)
from .prefab_policy import bot_prefab_is_suitable, is_zombie_prefab
from .profiles import ProfileFactory
from .supervisor import AIWorkerSupervisor, WorkerStatus

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player


logger = logging.getLogger(__name__)

_PLAYABLE_TEAMS = (TEAM1, TEAM2)
# A latched FIRE/MELEE/ORIENTED action may outlive its 400 ms intent TTL by
# this grace period while the aim motor finishes converging on the target.
_ACTION_CONVERGENCE_GRACE = 0.15
# The worker's native player-target claw gate (see _engage_zombie).  The
# director mirrors it for positionless melee so contact DPS is unchanged.
_PLAYER_MELEE_ALIGNMENT = 0.72
# Live target-lock fairness lease: about two worker confirmations at the
# 8 Hz decision cadence plus queue jitter.  While valid, the motor may refine
# the position of the worker-authorized visible target; when it lapses, aim
# reverts to the worker-frozen point and sustained fire stops.
_LOCK_LEASE = 0.45
_JUMP_PULSE_TICKS = 2
# Sustained tracking settles a human onto the target: perception lag and aim
# noise shrink over this many seconds of continuous lock.
_LOCK_SETTLE_TIME = 1.2
# Behavioral anti-wallbang probe budget: per-bot interval and a global
# per-tick cap so the 60 Hz gameplay thread never runs unbounded raycasts.
_WALL_PROBE_INTERVAL = 0.10
_WALL_PROBE_TICK_BUDGET = 4
# The retail protocol accepts up to 255 players, but one bot only needs its
# local/strategic cohort for a 10 Hz decision. Bounding this tuple keeps every
# PerceptionFrame below the frozen Windows pipe-record ceiling even on a full
# custom server; the authoritative server still owns and simulates all peers.
_MAX_PERCEPTION_PLAYERS = 32
# A PerceptionFrame is one multiprocessing pipe record. Keep enough room for
# the bounded player cohort and control metadata instead of allowing a dense
# custom map to recreate the frozen Windows large-write deadlock.
_MAX_PERCEPTION_ENTITIES = 192
_WALK_STEER_ANGLES = tuple(
    math.radians(value) for value in (20.0, 40.0, 60.0)
)
_DEFAULT_CLASSES = tuple(
    int(value)
    for value in (
        getattr(C, "CLASS_SOLDIER", -1),
        getattr(C, "CLASS_SCOUT", -1),
        getattr(C, "CLASS_ROCKETEER", -1),
        getattr(C, "CLASS_MINER", -1),
        getattr(C, "CLASS_ENGINEER", -1),
        getattr(C, "CLASS_SPECIALIST", -1),
        getattr(C, "CLASS_MEDIC", -1),
    )
    if int(value) in C.CLASS_ITEMS
)
_BOT_TRAVERSAL_PREFAB_TOKENS = (
    "bridge",
    "corridor",
    "ladder",
    "steps",
    "stair",
    "platform",
    "tube",
)
_BOT_COVER_PREFAB_TOKENS = (
    "barricade",
    "wall",
    "barrier",
    "bunker",
    "shield",
    "caltrop",
)


def _choose_bot_prefabs(
    available: list[str] | tuple[str, ...],
    rng: random.Random,
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    """Give each bot traversal and cover utility before optional variety."""

    pool = sorted({str(name) for name in available})
    if not pool or limit <= 0:
        return ()
    selection_limit = int(limit)
    if all(is_zombie_prefab(name) for name in pool):
        return tuple(rng.sample(pool, k=min(selection_limit, len(pool))))
    selected: list[str] = []
    for purpose, tokens in (
        ("traversal", _BOT_TRAVERSAL_PREFAB_TOKENS),
        ("cover", _BOT_COVER_PREFAB_TOKENS),
    ):
        if len(selected) >= selection_limit:
            break
        candidates = [
            name
            for name in pool
            if any(token in name.lower() for token in tokens)
            and bot_prefab_is_suitable(name, purpose)
        ]
        if candidates:
            choice = rng.choice(candidates)
            selected.append(choice)
            pool.remove(choice)
    pool = [name for name in pool if bot_prefab_is_suitable(name, "variety")]
    remaining = min(max(0, selection_limit - len(selected)), len(pool))
    if remaining:
        selected.extend(rng.sample(pool, k=remaining))
    return tuple(selected)


class _BotConnection:
    """Active peerless connection owned by the server rather than ENet."""

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server
        self.player: Player | None = None
        self.peer = None
        self.in_game = True
        self.map_sent = True
        self.known_entity_ids: set[int] = set()

    def send(self, data, reliable: bool = True, prefix: int = 0x30):
        """Discard owner-only packets; observers receive normal broadcasts."""

        return None

    def send_packet(self, packet, reliable: bool = True):
        """Packet-object compatibility for ``Player.send_packet``."""

        return None

    def disconnect(self, reason: int = 0) -> None:
        """Mark the peerless scene inactive; the director owns removal."""

        self.in_game = False


@dataclass(slots=True)
class _AimMotor:
    yaw: float
    pitch: float = 0.0
    yaw_velocity: float = 0.0
    pitch_velocity: float = 0.0
    yaw_noise: float = 0.0
    pitch_noise: float = 0.0


@dataclass(slots=True)
class _RuntimeBot:
    player: "Player"
    generation: int
    profile: BotProfile
    motor: _AimMotor
    rng: random.Random
    intent: BotIntent | None = None
    last_intent_frame: int = -1
    last_action_frame: int = -1
    feedback_action_kind: str = ""
    feedback_action_accepted: bool = True
    feedback_action_position: tuple[float, float, float] | None = None
    feedback_action_frame: int = -1
    feedback_action_at: float = 0.0
    next_perception_at: float = 0.0
    class_lives_remaining: int = 3
    action_primary: bool = False
    action_secondary: bool = False
    action_zoom: bool = False
    action_hover: bool = False
    action_primary_until_loop: int = -1
    movement_input: tuple[bool, bool, bool, bool, bool, bool, bool, bool] | None = None
    waypoint_probe_key: tuple | None = None
    waypoint_probe_result: bool = False
    was_alive: bool = True
    # Orientation-dependent action latched until the aim motor converges.
    pending_action: BotAction | None = None
    pending_action_look: tuple[float, float, float] | None = None
    pending_action_visible: bool = False
    pending_action_deadline: float = 0.0
    pending_action_priority: BotIntentPriority = BotIntentPriority.ROUTINE
    pending_action_secondary: bool = False
    pending_action_zoom: bool = False
    # Live target lock (fairness lease) and sustained-fire state.
    lock_player_id: int = -1
    lock_generation: int = 0
    lock_confirmed_at: float = 0.0
    lock_started_at: float = 0.0
    next_fire_at: float = 0.0
    next_wall_probe_at: float = 0.0
    wall_probe_clear: bool = True
    burst_remaining: int = 0
    last_jump_frame: int = -1
    jump_until_loop: int = -1
    jump_rearm_loop: int = -1


class BotDirector:
    """Own server-side bot identities and apply worker intentions.

    Thread/tick context: all public lifecycle and ``update`` methods run on the
    authoritative asyncio/gameplay thread.  They perform no process I/O and no
    AI raycasts/pathfinding.  The bridge/supervisor owns those operations.
    """

    def __init__(
        self,
        server: "BattleSpadesServer",
        supervisor: AIWorkerSupervisor | None = None,
    ) -> None:
        self.server = server
        config = self._config
        seed = int(getattr(config, "seed", 0))
        self.supervisor = supervisor or AIWorkerSupervisor(
            seed=seed,
            decision_hz=float(getattr(config, "decision_hz", 8.0)),
            path_requests_per_second=float(
                getattr(config, "path_requests_per_second", 24.0)
            ),
        )
        self.gateway = BotActionGateway(server)
        self.profile_factory = ProfileFactory(seed=seed)
        self._rng = random.Random(seed)
        self.bots: list[Player] = []
        self._runtime: dict[int, _RuntimeBot] = {}
        self._generation_counter: dict[int, int] = {}
        self._observed_player_objects: dict[int, tuple[int, int]] = {}
        self._frame_id = 0
        self._map_epoch = 0
        self._mode_epoch = 0
        self._topology_version = int(
            getattr(server.world_manager, "topology_version", 0)
        )
        self._map_signature = None
        self._mode_signature = None
        self._next_population_at = 0.0
        self._started = False
        self._mutation_subscription = None
        self._wall_probes_this_tick = 0

    @property
    def _config(self):
        return getattr(self.server.config, "bots", self.server.config)

    async def start(self, initial_count: int | None = None) -> None:
        """Start the worker and create the configured initial population."""

        if self._started:
            return
        self._started = True
        self._refresh_epochs(force=True)
        self.supervisor.start(self._make_map_snapshot(current=False))
        subscribe = getattr(self.server.world_manager, "subscribe_mutations", None)
        if callable(subscribe):
            self._mutation_subscription = subscribe(self._on_world_mutation)

        if initial_count is None:
            population_mode = str(
                getattr(self._config, "population_mode", "backfill")
            ).lower()
            initial_count = (
                int(getattr(self._config, "fill_target", 12))
                if population_mode == "backfill"
                else int(getattr(self._config, "max_bots", 12))
            )
        for _ in range(max(0, int(initial_count))):
            if await self.add_bot() is None:
                break

    async def close(self) -> None:
        """Retire bots, unsubscribe from VXL changes, and reap the worker."""

        for bot in list(self.bots):
            await self.remove_bot(bot, force=True)
        unsubscribe = getattr(self.server.world_manager, "unsubscribe_mutations", None)
        if callable(unsubscribe) and self._mutation_subscription is not None:
            unsubscribe(self._mutation_subscription)
        self._mutation_subscription = None
        self.supervisor.close()
        self._started = False

    async def add_bot(
        self,
        team: int | None = None,
        name: str | None = None,
        class_id: int | None = None,
        difficulty: str | None = None,
    ) -> "Player | None":
        """Create one mode-aware bot through the ordinary join boundaries."""

        from server.player import Player

        if len(self.bots) >= int(getattr(self._config, "max_bots", 12)):
            return None
        player_id = self.server.get_next_player_id()
        if player_id < 0:
            logger.warning("BotDirector: no free player id")
            return None
        requested_team = int(team) if team in _PLAYABLE_TEAMS else self._balanced_team()
        prepare_team = getattr(self.server.mode, "prepare_join_team", None)
        selected_team = (
            int(prepare_team(requested_team))
            if callable(prepare_team)
            else requested_team
        )
        if selected_team not in self.server.teams:
            return None

        profile = self.profile_factory.create(
            difficulty or str(getattr(self._config, "difficulty", "mixed"))
        )
        if name:
            profile = BotProfile(
                **{
                    **{
                        field_name: getattr(profile, field_name)
                        for field_name in profile.__dataclass_fields__
                    },
                    "name": str(name)[:15],
                }
            )
        selected_class = (
            int(class_id)
            if class_id is not None
            else self._choose_class(selected_team, profile)
        )
        from server.prefabs import allowed_prefabs_for_class

        available_prefabs = sorted(allowed_prefabs_for_class(selected_class))
        selected_prefabs = _choose_bot_prefabs(
            available_prefabs,
            self._rng,
        )
        from server.class_selection import normalize_server_selection
        selection = normalize_server_selection(
            self.server.config, selected_class, prefabs=selected_prefabs
        )
        prepare_selection = getattr(self.server.mode, "prepare_join_selection", None)
        if callable(prepare_selection):
            selection = prepare_selection(selected_team, selection)
        # Some modes own internal server-only variants which the retail class
        # picker cannot expose (Fast/Jump Zombie have no picker icons).  This
        # hook runs before Player construction and CreatePlayer, preventing a
        # post-broadcast class/model split for peerless bots.
        prepare_bot_selection = getattr(
            self.server.mode, "prepare_bot_selection", None
        )
        if callable(prepare_bot_selection):
            selection = prepare_bot_selection(
                selected_team,
                selection,
                player_id=player_id,
            )

        connection = _BotConnection(self.server)
        player = Player(
            player_id,
            profile.name,
            selected_team,
            DEFAULT_WEAPON_TOOL,
            connection,
        )
        connection.player = player
        player.is_bot = True
        player.apply_class_selection(selection)
        self._select_spawn_weapon(player)

        self.server.players[player_id] = player
        self.server.teams[selected_team].add_player(player)
        generation = self._generation_counter.get(player_id, 0) + 1
        self._generation_counter[player_id] = generation
        setattr(player, "bot_generation", generation)

        from server.round_lifecycle import resolve_player_spawn

        spawn = resolve_player_spawn(self.server, player)
        player.spawn(*spawn)
        self._select_spawn_weapon(player)
        # Bots have no retail ClientData stream to supply the stock display
        # bit. Without this, every observer hides the equipped gun even though
        # the authoritative tool and ammo are valid.
        player.update_action_input(
            False,
            False,
            can_display_weapon=True,
        )
        self.server._broadcast_create_player(player, spawn)
        player.restock_ammo()

        yaw = math.atan2(float(player.o_y), float(player.o_x))
        runtime = _RuntimeBot(
            player=player,
            generation=generation,
            profile=profile,
            motor=_AimMotor(yaw=yaw),
            rng=random.Random(self._rng.randrange(0, 2**31)),
        )
        phase = len(self.bots) / max(1.0, float(getattr(self._config, "perception_hz", 10)))
        runtime.next_perception_at = time.monotonic() + phase / max(1, int(getattr(self._config, "max_bots", 12)))
        self.bots.append(player)
        self._runtime[player_id] = runtime

        if self.server.mode is not None:
            await self.server.mode.on_player_join(player)
        logger.info(
            "Bot joined: %s id=%s generation=%s team=%s class=%s difficulty=%s",
            player.name,
            player.id,
            generation,
            player.team,
            player.class_id,
            profile.difficulty,
        )
        return player

    async def remove_bot(self, bot: "Player", *, force: bool = False) -> bool:
        """Retire one bot after objective-safe checks and full cleanup."""

        if bot not in self.bots:
            return False
        if not force and not self._safe_to_retire(bot):
            return False
        runtime = self._runtime.pop(int(bot.id), None)
        self.bots.remove(bot)
        self.server.round_lifecycle.forget_player(bot)
        if bot.team in self.server.teams:
            self.server.teams[bot.team].remove_player(bot)
        self.server.players.pop(bot.id, None)
        connection = getattr(bot, "connection", None)
        if connection is not None:
            connection.in_game = False

        packet = PlayerLeft()
        packet.player_id = int(bot.id)
        self.server.broadcast(bytes(packet.generate()))
        if self.server.mode is not None:
            await self.server.mode.on_player_leave(bot)
        if runtime is not None:
            self.profile_factory.release_name(runtime.profile.name)
        logger.info("Bot retired: %s id=%s", bot.name, bot.id)
        return True

    async def update(self, dt: float) -> None:
        """Drain intentions, publish staggered frames, and run cheap motors."""

        if not self._started:
            return
        now = time.monotonic()
        self._refresh_epochs()
        # Population work is a one-Hz policy. Avoid creating/awaiting a
        # coroutine on the other 59 fixed ticks each second.
        if now >= self._next_population_at:
            await self._maintain_population(now)
        self._drain_intents(now)
        self._publish_due_perception(now)
        fixed_dt = float(dt)
        self._wall_probes_this_tick = 0
        # None of the motor calls mutates the runtime dictionary. Iterating its
        # live values avoids allocating a 12-row tuple at 60 Hz.
        for runtime in self._runtime.values():
            if runtime.was_alive != bool(runtime.player.alive):
                self._update_class_lifetime(runtime)
            self._apply_motor(runtime, now, fixed_dt)

    def status(self) -> WorkerStatus:
        """Expose the worker portion of operational status."""

        return self.supervisor.status()

    def debug_snapshot(self, name: str | None = None) -> tuple[dict, ...]:
        """Return bounded current intent/path state for opt-in diagnostics."""

        if not bool(getattr(self._config, "debug_visualization", False)):
            return ()
        prefix = str(name or "").lower()
        rows = []
        for runtime in tuple(self._runtime.values())[:32]:
            player = runtime.player
            if prefix and not player.name.lower().startswith(prefix):
                continue
            intent = runtime.intent
            rows.append(
                {
                    "id": int(player.id),
                    "name": str(player.name),
                    "action": (
                        intent.action.kind.value if intent is not None else "idle"
                    ),
                    "affordance": (
                        intent.movement.affordance.value if intent is not None else "walk"
                    ),
                    "goal": intent.debug_goal if intent is not None else None,
                    "path": intent.debug_path if intent is not None else (),
                    "role": intent.debug_role if intent is not None else "idle",
                }
            )
        return tuple(rows)

    def request_population_refresh(self) -> None:
        """Ask the next tick to apply updated population configuration."""

        self._next_population_at = 0.0

    def _refresh_epochs(self, *, force: bool = False) -> None:
        world = self.server.world_manager
        map_signature = (
            getattr(world, "map_name", ""),
            int(getattr(world, "map_file_crc", 0)),
            id(getattr(world, "map", None)),
        )
        mode_signature = id(getattr(self.server, "mode", None))
        if force or map_signature != self._map_signature:
            self._map_signature = map_signature
            self._map_epoch += 1
            self._topology_version = int(getattr(world, "topology_version", 0))
            if self._started and not force:
                self.supervisor.publish_map(self._make_map_snapshot(current=False))
        if force or mode_signature != self._mode_signature:
            self._mode_signature = mode_signature
            self._mode_epoch += 1

    def _make_map_snapshot(self, *, current: bool) -> MapSnapshot:
        world = self.server.world_manager
        raw = bytes(getattr(world, "map_raw_bytes", b"") or b"")
        return MapSnapshot(
            map_epoch=self._map_epoch,
            topology_version=self._topology_version,
            raw_vxl=raw,
            mode_id=self._mode_id(),
            map_name=str(getattr(world, "map_name", "")),
        )

    def _on_world_mutation(
        self,
        x: int,
        y: int,
        z: int,
        solid: bool,
        color: int,
        topology_version: int,
    ) -> None:
        self._topology_version = max(self._topology_version, int(topology_version))
        self.supervisor.publish_world_change(
            VoxelChange(int(x), int(y), int(z), bool(solid), int(color)),
            map_epoch=self._map_epoch,
            topology_version=self._topology_version,
        )

    async def _maintain_population(self, now: float) -> None:
        if now < self._next_population_at:
            return
        self._next_population_at = now + 1.0
        config = self._config
        mode = str(getattr(config, "population_mode", "backfill")).lower()
        humans = sum(
            1 for player in self.server.players.values()
            if not bool(getattr(player, "is_bot", False))
        )
        maximum = max(0, int(getattr(config, "max_bots", 12)))
        if mode == "fixed":
            desired = maximum
        elif mode == "admin":
            return
        else:
            desired = max(0, int(getattr(config, "fill_target", 12)) - humans)
            reserved = max(0, int(getattr(config, "reserve_human_slots", 2)))
            desired = min(
                desired,
                max(0, int(self.server.config.max_players) - humans - reserved),
            )
        desired = min(desired, maximum)
        while len(self.bots) < desired:
            if await self.add_bot() is None:
                break
        excess = len(self.bots) - desired
        if excess <= 0:
            return
        candidates = sorted(
            self.bots,
            key=lambda player: (
                bool(getattr(player, "alive", False)),
                bool(getattr(player, "pickup_id", None) is not None),
                int(player.id),
            ),
        )
        for candidate in candidates:
            if excess <= 0:
                break
            if await self.remove_bot(candidate):
                excess -= 1

    def _publish_due_perception(self, now: float) -> None:
        if not self.bots:
            return
        interval = 1.0 / max(1.0, float(getattr(self._config, "perception_hz", 10)))
        due = [
            (bot, self._runtime.get(int(bot.id)))
            for bot in tuple(self.bots)
            if (
                self._runtime.get(int(bot.id)) is not None
                and now >= self._runtime[int(bot.id)].next_perception_at
            )
        ]
        if not due:
            return
        players = self._snapshot_players()
        entities = self._snapshot_entities()
        objectives = self._snapshot_objectives()
        mode_id = self._mode_id()
        mode_phase = self._mode_phase()
        for bot, runtime in due:
            runtime.next_perception_at = now + interval
            self._frame_id += 1
            self.supervisor.submit_frame(
                PerceptionFrame(
                    frame_id=self._frame_id,
                    map_epoch=self._map_epoch,
                    mode_epoch=self._mode_epoch,
                    topology_version=self._topology_version,
                    observer_id=int(bot.id),
                    observer_generation=runtime.generation,
                    created_at=now,
                    mode_id=mode_id,
                    players=self._players_for_observer(
                        players,
                        observer_id=int(bot.id),
                        objectives=objectives,
                    ),
                    profile=runtime.profile,
                    entities=entities,
                    objectives=objectives,
                    mode_phase=mode_phase,
                    stimuli=(
                        self.server.bot_stimuli.perceive(
                            tuple(float(value) for value in bot.position),
                            now=now,
                            rng=runtime.rng,
                        )
                        if getattr(self.server, "bot_stimuli", None) is not None
                        else ()
                    ),
                )
            )

    @staticmethod
    def _players_for_observer(
        players: tuple[PlayerSnapshot, ...],
        *,
        observer_id: int,
        objectives: tuple[ObjectiveSnapshot, ...],
    ) -> tuple[PlayerSnapshot, ...]:
        """Select one bounded strategic cohort for a bot perception frame.

        The observer, every server-owned bot, and objective carriers outrank
        ordinary peers. Remaining slots are the nearest live participants,
        with player id as a deterministic tie-breaker.
        """

        if len(players) <= _MAX_PERCEPTION_PLAYERS:
            return players
        observer = next(
            (
                player
                for player in players
                if int(player.player_id) == int(observer_id)
            ),
            None,
        )
        origin = observer.position if observer is not None else (0.0, 0.0, 0.0)
        carriers = {
            int(objective.carrier_id)
            for objective in objectives
            if int(objective.carrier_id) >= 0
        }

        def priority(player: PlayerSnapshot) -> tuple[float, float, int]:
            player_id = int(player.player_id)
            if player_id == int(observer_id):
                rank = 0.0
            elif player_id in carriers:
                rank = 1.0
            elif bool(player.is_bot):
                rank = 2.0
            elif bool(player.alive) and bool(player.spawned):
                rank = 3.0
            else:
                rank = 4.0
            distance = (
                (player.position[0] - origin[0]) ** 2
                + (player.position[1] - origin[1]) ** 2
                + (player.position[2] - origin[2]) ** 2
            )
            return rank, distance, player_id

        return tuple(
            nsmallest(
                _MAX_PERCEPTION_PLAYERS,
                players,
                key=priority,
            )
        )

    def _snapshot_players(self) -> tuple[PlayerSnapshot, ...]:
        snapshots: list[PlayerSnapshot] = []
        for player in tuple(self.server.players.values()):
            generation = self._player_generation(player)
            runtime = self._runtime.get(int(player.id))
            snapshots.append(
                PlayerSnapshot(
                    player_id=int(player.id),
                    generation=generation,
                    team=int(getattr(player, "team", -1)),
                    class_id=int(getattr(player, "class_id", -1)),
                    alive=bool(getattr(player, "alive", False)),
                    spawned=bool(getattr(player, "spawned", False)),
                    position=tuple(float(value) for value in player.position),
                    eye=tuple(float(value) for value in player.eye),
                    orientation=tuple(float(value) for value in player.orientation),
                    velocity=(
                        float(getattr(player, "vx", 0.0)),
                        float(getattr(player, "vy", 0.0)),
                        float(getattr(player, "vz", 0.0)),
                    ),
                    health=int(getattr(player, "health", 0)),
                    tool=int(getattr(player, "tool", -1)),
                    blocks=int(getattr(player, "blocks", 0)),
                    ammo_clip=int(getattr(player, "ammo_clip", 0)),
                    ammo_reserve=int(getattr(player, "ammo_reserve", 0)),
                    is_bot=bool(getattr(player, "is_bot", False)),
                    weapon_tool=int(getattr(player, "weapon", -1)),
                    loadout=tuple(
                        int(tool)
                        for tool in (getattr(player, "loadout", ()) or ())
                    ),
                    prefabs=tuple(
                        str(name)
                        for name in (getattr(player, "prefabs", ()) or ())
                    ),
                    oriented_stock=tuple(
                        (int(tool), int(stock))
                        for tool, stock in sorted(
                            (getattr(player, "oriented_stock", {}) or {}).items()
                        )
                    ),
                    carried_entity_id=int(
                        getattr(player, "pickup_id", -1)
                        if getattr(player, "pickup_id", None) is not None
                        else -1
                    ),
                    jetpack_id=int(getattr(player, "jetpack_id", 0)),
                    jetpack_fuel=float(getattr(player, "jetpack_fuel", 0.0)),
                    grounded=bool(getattr(player, "grounded", False)),
                    wade=bool(getattr(player, "wade", False)),
                    reloading=bool(getattr(player, "reloading", False)),
                    last_action_kind=(
                        runtime.feedback_action_kind if runtime is not None else ""
                    ),
                    last_action_accepted=(
                        runtime.feedback_action_accepted
                        if runtime is not None
                        else True
                    ),
                    last_action_position=(
                        runtime.feedback_action_position
                        if runtime is not None
                        else None
                    ),
                    last_action_frame=(
                        runtime.feedback_action_frame if runtime is not None else -1
                    ),
                    last_action_at=(
                        runtime.feedback_action_at if runtime is not None else 0.0
                    ),
                    last_damage_at=float(
                        getattr(player, "_last_combat_damage_at", 0.0)
                    ),
                    last_damage_source_id=int(
                        getattr(player, "_last_damage_source_id", -1)
                    ),
                    last_damage_source_position=(
                        tuple(
                            float(value)
                            for value in player._last_damage_source_position
                        )
                        if getattr(
                            player, "_last_damage_source_position", None
                        )
                        is not None
                        else None
                    ),
                    life_id=int(getattr(player, "deaths", 0)),
                )
            )
        return tuple(snapshots)

    def _snapshot_entities(self) -> tuple[EntitySnapshot, ...]:
        registry = getattr(self.server, "entity_registry", None)
        if registry is None:
            return ()
        explosive_types = {
            int(getattr(C, "DYNAMITE_ENTITY", 10)): int(C.DYNAMITE_TOOL),
            int(getattr(C, "LANDMINE_ENTITY", 9)): int(C.LANDMINE_TOOL),
            int(getattr(C, "C4_ENTITY", 38)): int(C.C4_TOOL),
        }
        result: list[EntitySnapshot] = []
        for entity in tuple(registry.all()):
            kind = str(getattr(entity, "kind", ""))
            # The projectile engine owns the current moving coordinates.  Its
            # registry counterpart retains only the spawn transform for wire
            # replication and would make bots dodge a stale location.
            if kind == "projectile":
                continue
            # Dead pickups remain registered while waiting to respawn. They
            # are neither visible nor usable and only inflate every bot frame.
            if not bool(getattr(entity, "alive", True)):
                continue
            position = getattr(entity, "position", None)
            if position is None:
                position = (
                    getattr(entity, "x", 0.0),
                    getattr(entity, "y", 0.0),
                    getattr(entity, "z", 0.0),
                )
            entity_type = int(
                getattr(entity, "entity_type", getattr(entity, "type", -1))
            )
            behavior = getattr(entity, "behavior", None)
            blast_radius = float(getattr(behavior, "blast_radius", 0.0) or 0.0)
            detonate_at = float(
                getattr(behavior, "_detonate_at", 0.0) or 0.0
            )
            result.append(
                EntitySnapshot(
                    entity_id=int(getattr(entity, "entity_id", -1)),
                    entity_type=entity_type,
                    team=int(getattr(entity, "team", -1)),
                    owner_id=int(getattr(entity, "player_id", -1)),
                    position=tuple(float(value) for value in position),
                    alive=bool(getattr(entity, "alive", True)),
                    kind=kind,
                    tool_id=explosive_types.get(entity_type, -1),
                    velocity=tuple(
                        float(value)
                        for value in getattr(entity, "vel", (0.0, 0.0, 0.0))
                    ),
                    blast_radius=blast_radius,
                    detonate_at=detonate_at,
                    hazardous=(
                        bool(getattr(entity, "alive", True))
                        and entity_type in explosive_types
                        and blast_radius > 0.0
                    ),
                )
            )
        engine = getattr(self.server, "projectile_engine", None)
        projectiles = tuple(getattr(engine, "projectiles", ()) or ())
        players = getattr(self.server, "players", {})
        for index, projectile in enumerate(projectiles):
            spec = getattr(projectile, "spec", None)
            if spec is None:
                continue
            owner_id = int(getattr(projectile, "thrower_id", -1))
            owner = players.get(owner_id)
            blast_radius = float(getattr(spec, "blast_radius", 0.0) or 0.0)
            entity_id = getattr(projectile, "entity_id", None)
            result.append(
                EntitySnapshot(
                    entity_id=(
                        int(entity_id) if entity_id is not None else -1 - index
                    ),
                    entity_type=int(getattr(spec, "entity_type", -1) or -1),
                    team=int(getattr(owner, "team", -1)),
                    owner_id=owner_id,
                    position=(
                        float(projectile.x),
                        float(projectile.y),
                        float(projectile.z),
                    ),
                    alive=True,
                    kind="projectile",
                    tool_id=int(getattr(projectile, "tool", -1)),
                    velocity=(
                        float(projectile.vx),
                        float(projectile.vy),
                        float(projectile.vz),
                    ),
                    blast_radius=blast_radius,
                    detonate_at=float(
                        getattr(projectile, "explode_at", 0.0)
                        or getattr(projectile, "lifespan_at", 0.0)
                        or 0.0
                    ),
                    hazardous=(
                        float(getattr(spec, "damage", 0.0) or 0.0) > 0.0
                        and blast_radius > 0.0
                    ),
                )
            )
        if len(result) <= _MAX_PERCEPTION_ENTITIES:
            return tuple(result)

        carried_ids = {
            int(pickup_id)
            for player in tuple(players.values())
            for pickup_id in (getattr(player, "pickup_id", None),)
            if pickup_id is not None
        }
        live_positions = tuple(
            tuple(float(value) for value in player.position)
            for player in tuple(players.values())
            if bool(getattr(player, "alive", False))
            and bool(getattr(player, "spawned", False))
            and getattr(player, "position", None) is not None
        )

        def priority(snapshot: EntitySnapshot) -> tuple[float, float, int]:
            kind = snapshot.kind.lower()
            if snapshot.hazardous:
                rank = 0.0
            elif snapshot.entity_id in carried_ids:
                rank = 1.0
            elif kind in {"objective", "intel", "base"}:
                rank = 2.0
            elif kind == "projectile":
                rank = 3.0
            elif kind in {"pickup", "crate", "resource"}:
                rank = 4.0
            else:
                rank = 5.0
            distance = min(
                (
                    (snapshot.position[0] - position[0]) ** 2
                    + (snapshot.position[1] - position[1]) ** 2
                    + (snapshot.position[2] - position[2]) ** 2
                    for position in live_positions
                ),
                default=0.0,
            )
            return rank, distance, int(snapshot.entity_id)

        selected = nsmallest(
            _MAX_PERCEPTION_ENTITIES,
            result,
            key=priority,
        )
        metrics = getattr(self.server, "metrics", None)
        if metrics is not None:
            metrics.bot_perception_entity_overflow += len(result) - len(selected)
        return tuple(selected)

    def _snapshot_objectives(self) -> tuple[ObjectiveSnapshot, ...]:
        mode = getattr(self.server, "mode", None)
        if mode is None:
            return ()
        result: list[ObjectiveSnapshot] = []

        base_positions = getattr(mode, "base_positions", None)
        intel_positions = getattr(mode, "intel_positions", None)
        intel_holders = getattr(mode, "intel_holder", None)
        if isinstance(base_positions, dict) and isinstance(intel_positions, dict):
            for team in _PLAYABLE_TEAMS:
                base = base_positions.get(team)
                if base is not None:
                    result.append(
                        ObjectiveSnapshot(
                            "ctf_base",
                            team,
                            tuple(float(value) for value in base),
                        )
                    )
                intel = intel_positions.get(team)
                holder = (
                    intel_holders.get(team)
                    if isinstance(intel_holders, dict)
                    else None
                )
                if holder is not None:
                    intel = holder.position
                if intel is not None:
                    dropped_at = float(
                        getattr(mode, "intel_drop_time", {}).get(team, 0.0)
                    )
                    result.append(
                        ObjectiveSnapshot(
                            "ctf_intel",
                            team,
                            tuple(float(value) for value in intel),
                            carrier_id=int(getattr(holder, "id", -1)),
                            state=2 if holder is not None else (1 if dropped_at > 0.0 else 0),
                        )
                    )

        # Every team mode needs a strategic destination outside visual range.
        # These stable authored/dry base anchors are ordinary map knowledge,
        # not hidden live-player positions. Without them TDM teams spawned 400
        # blocks apart and random-patrolled forever because perception is
        # correctly capped at 160 blocks.
        anchor_reader = getattr(self.server.world_manager, "team_base_anchor", None)
        if callable(anchor_reader):
            for team in _PLAYABLE_TEAMS:
                try:
                    anchor = anchor_reader(team)
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    continue
                result.append(
                    ObjectiveSnapshot(
                        "team_anchor",
                        team,
                        tuple(float(value) for value in anchor),
                    )
                )

        vips = getattr(mode, "vips", None)
        if isinstance(vips, dict):
            for team, vip in vips.items():
                if vip is not None and bool(getattr(vip, "alive", False)):
                    result.append(
                        ObjectiveSnapshot(
                            "vip",
                            int(team),
                            tuple(float(value) for value in vip.position),
                            carrier_id=int(vip.id),
                        )
                    )

        last_survivor_id = getattr(mode, "last_survivor_id", None)
        if last_survivor_id is not None:
            survivor = self.server.players.get(int(last_survivor_id))
            if survivor is not None:
                result.append(
                    ObjectiveSnapshot(
                        "last_survivor",
                        int(survivor.team),
                        tuple(float(value) for value in survivor.position),
                        carrier_id=int(survivor.id),
                    )
                )
        return tuple(result)

    def _player_generation_readonly(self, player: "Player") -> int:
        """Read a player's generation without registering new observations.

        Unknown humans return -1 so a live lock can never bind to a player
        object that perception snapshots have not yet described.
        """

        if bool(getattr(player, "is_bot", False)):
            return int(getattr(player, "bot_generation", 0))
        observed = self._observed_player_objects.get(int(player.id))
        if observed is None or observed[0] != id(player):
            return -1
        return observed[1]

    def _player_generation(self, player: "Player") -> int:
        if bool(getattr(player, "is_bot", False)):
            return int(getattr(player, "bot_generation", 0))
        player_id = int(player.id)
        identity = id(player)
        previous = self._observed_player_objects.get(player_id)
        if previous is None or previous[0] != identity:
            generation = 1 if previous is None else previous[1] + 1
            self._observed_player_objects[player_id] = identity, generation
            return generation
        return previous[1]

    def _drain_intents(self, now: float) -> None:
        for intent in self.supervisor.drain_intents(limit=12):
            runtime = self._runtime.get(int(intent.bot_id))
            if runtime is None:
                continue
            if (
                intent.bot_generation != runtime.generation
                or intent.map_epoch != self._map_epoch
                or intent.mode_epoch != self._mode_epoch
                or intent.topology_version != self._topology_version
                or intent.expires_at <= now
                or intent.frame_id <= runtime.last_intent_frame
            ):
                continue
            if (
                runtime.pending_action is not None
                and int(intent.priority) > int(runtime.pending_action_priority)
            ):
                # A pending dig/breach is allowed a short aim-convergence
                # grace, but it must never survive a newer combat or survival
                # interrupt and re-select the old terrain tool afterwards.
                self._clear_pending_action(runtime)
            runtime.intent = intent
            runtime.last_intent_frame = int(intent.frame_id)
            look = intent.look
            if (
                look is not None
                and look.visible
                and int(getattr(look, "target_player_id", -1)) >= 0
            ):
                # The worker confirmed a visible target: refresh the live
                # tracking lease.  Absence of confirmations lets it lapse;
                # nothing here ever clears worker-side knowledge.
                target_id = int(look.target_player_id)
                if target_id != runtime.lock_player_id:
                    runtime.lock_started_at = now
                    runtime.wall_probe_clear = True
                    runtime.next_wall_probe_at = 0.0
                    runtime.burst_remaining = 0
                    # Flick overshoot: snapping attention to a new target
                    # briefly overshoots in the turn direction; the OU decay
                    # then settles it. Experts overshoot less.
                    player = runtime.player
                    yaw_error = self._wrap(
                        math.atan2(
                            float(look.target[1]) - float(player.eye_y),
                            float(look.target[0]) - float(player.eye_x),
                        )
                        - runtime.motor.yaw
                    )
                    if abs(yaw_error) > 0.35:
                        runtime.motor.yaw_noise += (
                            math.copysign(1.0, yaw_error)
                            * runtime.rng.uniform(0.05, 0.18)
                            * (1.0 - float(runtime.profile.skill))
                        )
                runtime.lock_player_id = target_id
                runtime.lock_generation = int(
                    getattr(look, "target_generation", 0)
                )
                runtime.lock_confirmed_at = now

    def _apply_motor(self, runtime: _RuntimeBot, now: float, dt: float) -> None:
        player = runtime.player
        intent = runtime.intent
        if not player.alive or not player.spawned:
            self._clear_pending_action(runtime)
        if (
            not player.alive
            or not player.spawned
            or intent is None
            or intent.expires_at <= now
        ):
            runtime.jump_until_loop = -1
            runtime.jump_rearm_loop = -1
            # A latched action legitimately outlives its intent by the
            # convergence grace window: keep slewing toward its target so a
            # nearly-faced dig or shot still lands before the deadline.
            pending_goal = self._pending_look_goal(runtime)
            if pending_goal is not None:
                self._update_aim(runtime, pending_goal, dt)
                self._try_pending_action(runtime, now)
            self._set_movement_state(runtime, (False,) * 8)
            held_primary = (
                bool(player.alive)
                and bool(player.spawned)
                and int(getattr(self.server, "loop_count", 0))
                <= runtime.action_primary_until_loop
            )
            self._set_action_state(
                runtime,
                primary=held_primary,
                secondary=(
                    runtime.pending_action_secondary
                    if runtime.pending_action is not None
                    else False
                ),
                zoom=(
                    runtime.pending_action_zoom
                    if runtime.pending_action is not None
                    else False
                ),
                hover=False,
            )
            return
        action_due = (
            intent.action.kind is not BotActionKind.NONE
            and runtime.last_action_frame != intent.frame_id
        )
        requested_tool = int(getattr(intent, "tool_id", -1))
        if requested_tool >= 0 and int(getattr(player, "tool", -1)) != requested_tool:
            # Tool choice is part of the worker intention, not only an action
            # side effect. A threatened bot must visibly draw its gun during
            # reaction time instead of standing with blocks/prefabs equipped.
            self.gateway.select_tool(player, requested_tool)
        if action_due:
            runtime.last_action_frame = int(intent.frame_id)
            if intent.action.kind in (
                BotActionKind.FIRE,
                BotActionKind.MELEE,
                BotActionKind.ORIENTED,
            ):
                # These gateway calls shoot along the CURRENT orientation.
                # Executing them on the arrival tick fired digs and shots at
                # whatever the bot still happened to face. Latch instead and
                # execute from _try_pending_action once the aim converges.
                self._latch_action(runtime, intent)
            else:
                # BUILD/PLACE_PREFAB/DEPLOY/RELOAD carry explicit positions
                # (or need none) and are orientation-independent.
                accepted = self.gateway.execute(player, intent.action)
                self._record_action_result(
                    runtime, intent.action, accepted, now
                )
        pending = runtime.pending_action
        if pending is not None and pending.position is not None:
            # A world-cell action keeps aim priority over the newest look so
            # the swing converges even while the worker already looks ahead.
            self._update_aim(runtime, pending.position, dt)
        elif intent.look is not None:
            aim_point, settle = self._live_aim_point(runtime, intent.look, now)
            # Skill limits how tightly sustained tracking settles: a casual
            # hand keeps wandering, an expert locks in.
            skill_settle = settle * (
                0.35 + 0.65 * float(runtime.profile.skill)
            )
            self._update_aim(
                runtime,
                aim_point,
                dt,
                noise_factor=1.0 / (1.0 + 2.0 * skill_settle),
            )
        elif runtime.pending_action_look is not None:
            self._update_aim(runtime, runtime.pending_action_look, dt)
        # Scope/secondary state must be authoritative before a converged FIRE
        # executes below; otherwise the first sniper round is sent as hip fire
        # and remote clients miss the beam for that shot.
        self._set_action_state(
            runtime,
            primary=runtime.action_primary,
            secondary=bool(intent.secondary_fire),
            zoom=bool(intent.zoom),
            hover=runtime.action_hover,
        )
        self._try_pending_action(runtime, now)
        direction = self._live_movement_direction(
            runtime,
            intent.movement.direction,
            intent.movement.affordance,
        )
        forward = (float(player.o_x), float(player.o_y))
        side = (-forward[1], forward[0])
        forward_amount = direction[0] * forward[0] + direction[1] * forward[1]
        side_amount = direction[0] * side[0] + direction[1] * side[1]
        affordance = intent.movement.affordance
        jetpack_requested = (
            affordance is MovementAffordance.JETPACK
            and int(getattr(player, "jetpack_id", 0)) > 0
            and float(getattr(player, "jetpack_fuel", 0.0)) > 0.0
        )
        primary_latched = (
            int(getattr(self.server, "loop_count", 0))
            <= runtime.action_primary_until_loop
        )
        jump_requested = (
            bool(intent.movement.jump)
            or affordance is MovementAffordance.JUMP
        )
        current_loop = int(getattr(self.server, "loop_count", 0))
        wading_jump = bool(getattr(player, "wade", False)) and jump_requested
        if wading_jump:
            # Native swimming needs held ascent. Ground jumps remain bounded
            # pulses, but pulsing only a few ticks per worker frame made bots
            # bob and crawl across large water maps.
            runtime.jump_until_loop = -1
            runtime.jump_rearm_loop = -1
        elif (
            jump_requested
            and int(intent.frame_id) != runtime.last_jump_frame
            and current_loop >= runtime.jump_rearm_loop
        ):
            runtime.last_jump_frame = int(intent.frame_id)
            runtime.jump_until_loop = current_loop + _JUMP_PULSE_TICKS
            # A release window prevents adjacent worker frames from merging
            # into one native held-key interval.
            runtime.jump_rearm_loop = runtime.jump_until_loop + 2
        jump_held = wading_jump or current_loop <= runtime.jump_until_loop
        self._set_movement_state(runtime, (
            forward_amount > 0.25,
            forward_amount < -0.25,
            side_amount < -0.25,
            side_amount > 0.25,
            jump_held,
            bool(intent.movement.crouch)
            or affordance is MovementAffordance.CROUCH,
            bool(intent.movement.sneak),
            bool(intent.movement.sprint)
            and math.hypot(direction[0], direction[1]) > 0.1,
        ))
        self._set_action_state(
            runtime,
            primary=jetpack_requested
            or primary_latched,
            secondary=bool(intent.secondary_fire),
            zoom=bool(intent.zoom),
            hover=jetpack_requested,
        )

    @staticmethod
    def _clear_pending_action(runtime: _RuntimeBot) -> None:
        runtime.pending_action = None
        runtime.pending_action_look = None
        runtime.pending_action_visible = False
        runtime.pending_action_deadline = 0.0
        runtime.pending_action_priority = BotIntentPriority.ROUTINE
        runtime.pending_action_secondary = False
        runtime.pending_action_zoom = False

    @staticmethod
    def _latch_action(runtime: _RuntimeBot, intent: BotIntent) -> None:
        look = intent.look
        runtime.pending_action = intent.action
        runtime.pending_action_look = (
            tuple(float(value) for value in look.target)
            if look is not None
            else None
        )
        runtime.pending_action_visible = bool(look is not None and look.visible)
        runtime.pending_action_deadline = (
            float(intent.expires_at) + _ACTION_CONVERGENCE_GRACE
        )
        runtime.pending_action_priority = BotIntentPriority(intent.priority)
        runtime.pending_action_secondary = bool(intent.secondary_fire)
        runtime.pending_action_zoom = bool(intent.zoom)

    @staticmethod
    def _pending_look_goal(
        runtime: _RuntimeBot,
    ) -> tuple[float, float, float] | None:
        action = runtime.pending_action
        if action is None:
            return None
        if action.position is not None:
            return action.position
        return runtime.pending_action_look

    def _lock_target(self, runtime: _RuntimeBot, look, now: float):
        """Return the live locked Player while the fairness lease is valid.

        Fairness contract: the worker's LOS/FOV-gated perception is the only
        authority for WHO may be tracked and WHEN.  The director merely
        refines the position of an already-authorized, currently-visible
        target for at most _LOCK_LEASE seconds since the last worker
        confirmation.  No director-side information ever flows back to the
        worker, so this can never become a wallhack.
        """

        target_id = int(getattr(look, "target_player_id", -1))
        if target_id < 0 or target_id != runtime.lock_player_id:
            return None
        if now - runtime.lock_confirmed_at > _LOCK_LEASE:
            return None
        target = self.server.players.get(target_id)
        if (
            target is None
            or not bool(getattr(target, "alive", False))
            or not bool(getattr(target, "spawned", False))
        ):
            return None
        if self._player_generation_readonly(target) != runtime.lock_generation:
            return None
        return target

    def _live_aim_point(
        self, runtime: _RuntimeBot, look, now: float
    ) -> tuple[tuple[float, float, float], float]:
        """Resolve the aim point and settle factor for one look intent.

        Returns the worker-frozen point with settle 0.0 whenever no valid
        live lease exists.  Under a lease the point is the target's live eye
        pulled back by a settling perception lag, mimicking a human locking
        onto a tracked target.
        """

        target = self._lock_target(runtime, look, now)
        if target is None:
            return tuple(float(value) for value in look.target), 0.0
        settle = min(
            1.0, max(0.0, now - runtime.lock_started_at) / _LOCK_SETTLE_TIME
        )
        lag = float(runtime.profile.tracking_delay) * (1.0 - 0.7 * settle)
        return (
            (
                float(target.eye_x) - float(getattr(target, "vx", 0.0)) * lag,
                float(target.eye_y) - float(getattr(target, "vy", 0.0)) * lag,
                float(target.eye_z)
                - float(getattr(target, "vz", 0.0)) * lag
                + float(getattr(look, "aim_offset_z", 0.0)),
            ),
            settle,
        )

    def _fire_lane_clear(
        self,
        runtime: _RuntimeBot,
        delta: tuple[float, float, float],
        distance: float,
        now: float,
    ) -> bool:
        """Budgeted behavioral anti-wallbang probe for sustained fire.

        One probe per bot per _WALL_PROBE_INTERVAL and a global per-tick cap
        keep this off the 60 Hz critical path.  CombatSystem re-raycasts
        authoritatively on every shot, so a stale probe can only waste a
        bullet, never grant a hit through terrain.
        """

        if (
            distance > 2.0
            and now >= runtime.next_wall_probe_at
            and self._wall_probes_this_tick < _WALL_PROBE_TICK_BUDGET
        ):
            self._wall_probes_this_tick += 1
            runtime.next_wall_probe_at = now + _WALL_PROBE_INTERVAL
            raycast = getattr(self.server.world_manager, "raycast", None)
            clear = True
            if callable(raycast):
                player = runtime.player
                try:
                    hit = raycast(
                        float(player.eye_x),
                        float(player.eye_y),
                        float(player.eye_z),
                        delta[0] / distance,
                        delta[1] / distance,
                        delta[2] / distance,
                        min(distance - 1.0, 128.0),
                    )
                except (TypeError, ValueError):
                    hit = None
                clear = hit is None
            runtime.wall_probe_clear = clear
        return runtime.wall_probe_clear

    def _try_pending_action(self, runtime: _RuntimeBot, now: float) -> None:
        action = runtime.pending_action
        if action is None:
            return
        player = runtime.player
        if (
            not player.alive
            or not player.spawned
            or now > runtime.pending_action_deadline
        ):
            # Never execute "because time ran out": a dropped action simply
            # waits for the worker's next decision.
            self._clear_pending_action(runtime)
            return
        if action.kind is BotActionKind.FIRE and now < runtime.next_fire_at:
            # Sustained fire respects the weapon cadence between shots.
            return
        reference = action.position
        visible = True
        if reference is None:
            intent = runtime.intent
            if (
                intent is not None
                and intent.look is not None
                and intent.expires_at > now
            ):
                # Prefer the freshest worker look for player-target actions;
                # under a valid lease this resolves the live target position.
                reference, _settle = self._live_aim_point(
                    runtime, intent.look, now
                )
                visible = bool(intent.look.visible)
            else:
                reference = runtime.pending_action_look
                visible = runtime.pending_action_visible
        if action.kind is BotActionKind.FIRE and not visible:
            # FIRE stays gated on a fresh visible perception sample; hold and
            # let the deadline drop it if visibility never returns.
            # CombatSystem performs the final authoritative ray/terrain test.
            return
        if reference is None:
            # No aim reference at all: keep the historical immediate
            # execution (positionless self-actions and direct-driven tests).
            self._execute_pending(runtime, action, now)
            return
        dx = float(reference[0]) - float(player.eye_x)
        dy = float(reference[1]) - float(player.eye_y)
        dz = float(reference[2]) - float(player.eye_z)
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        if distance <= 1e-6:
            self._execute_pending(runtime, action, now)
            return
        cos_error = (
            float(player.o_x) * dx
            + float(player.o_y) * dy
            + float(player.o_z) * dz
        ) / distance
        if cos_error >= self._facing_threshold(runtime, action, distance):
            if action.kind is BotActionKind.FIRE and not self._fire_lane_clear(
                runtime, (dx, dy, dz), distance, now
            ):
                return
            self._execute_pending(runtime, action, now)

    @staticmethod
    def _facing_threshold(
        runtime: _RuntimeBot, action: BotAction, distance: float
    ) -> float:
        if action.position is not None:
            # World-cell target: the ray must actually intersect the block
            # (half extent 0.45) plus a small slack so orientation noise on
            # an already-converged aim cannot starve the swing forever.
            return math.cos(math.atan2(0.45, max(distance, 0.75)) + 0.03)
        if action.kind is BotActionKind.MELEE:
            # At point-blank range the angle to a torso point swings wildly
            # every tick; a swing there connects regardless of fine aim, so
            # taper the gate away instead of starving claws at 0.7 blocks.
            if distance <= 1.2:
                return 0.0
            if distance <= 2.5:
                return _PLAYER_MELEE_ALIGNMENT * ((distance - 1.2) / 1.3)
            return _PLAYER_MELEE_ALIGNMENT
        profile = runtime.profile
        # Skill narrows the acceptable error toward the target's half-width;
        # the aim-noise slack keeps casual profiles (large persistent OU
        # error) firing roughly on target instead of never converging.
        error_budget = (
            math.atan2(0.5, max(distance, 2.0))
            * (0.9 + 0.9 * (1.0 - float(profile.skill)))
            + float(profile.aim_noise) * 1.2
            + 0.01
        )
        return math.cos(min(error_budget, math.pi / 2.0))

    def _execute_pending(
        self, runtime: _RuntimeBot, action: BotAction, now: float
    ) -> None:
        accepted = self.gateway.execute(runtime.player, action)
        self._record_action_result(runtime, action, accepted, now)
        if accepted and action.kind is BotActionKind.FIRE:
            self._apply_recoil(runtime)
        sustain = (
            accepted
            and action.kind is BotActionKind.FIRE
            and runtime.lock_player_id >= 0
            and now - runtime.lock_confirmed_at <= _LOCK_LEASE
        )
        if sustain:
            # Weapons-free on the locked target: keep the pending FIRE armed
            # at the weapon's real cadence while the worker keeps confirming
            # visibility.  consume_shot stays the authoritative ROF/ammo
            # gate, so this can never over-fire.
            profile = WEAPON_PROFILES.get(
                int(getattr(runtime.player, "tool", -1))
            )
            interval = max(
                float(getattr(profile, "fire_interval", 0.0) or 0.0),
                1.0 / 30.0,
            )
            burst = int(getattr(action, "burst", 0) or 0)
            if burst > 0:
                if runtime.burst_remaining <= 0:
                    runtime.burst_remaining = burst
                runtime.burst_remaining -= 1
                if runtime.burst_remaining <= 0:
                    interval += max(
                        0.0, float(getattr(action, "burst_pause", 0.0))
                    )
            runtime.next_fire_at = now + interval
        else:
            self._clear_pending_action(runtime)
        if not accepted:
            return
        # Replication publishes at 30 Hz while this motor ticks at 60 Hz. A
        # one-tick primary pulse can fall entirely between snapshots, making
        # remote melee/mining animation and sound disappear. Hold through two
        # future loops so at least one WorldUpdate contains bit 0x01. The
        # hold is anchored to the EXECUTION tick so the visible swing
        # coincides with the authoritative dig/shot.
        runtime.action_primary_until_loop = max(
            runtime.action_primary_until_loop,
            int(getattr(self.server, "loop_count", 0)) + 2,
        )

    @staticmethod
    def _record_action_result(
        runtime: _RuntimeBot,
        action: BotAction,
        accepted: bool,
        now: float,
    ) -> None:
        """Publish one bounded authoritative result to worker perception."""

        runtime.feedback_action_kind = str(action.kind.value)
        runtime.feedback_action_accepted = bool(accepted)
        runtime.feedback_action_position = (
            tuple(float(value) for value in action.position)
            if action.position is not None
            else None
        )
        runtime.feedback_action_frame = int(runtime.last_action_frame)
        runtime.feedback_action_at = float(now)

    def _apply_recoil(self, runtime: _RuntimeBot) -> None:
        """Kick the aim motor per accepted shot; recoil_control mitigates.

        AoS z increases downward, so negative pitch noise lifts the muzzle.
        The OU decay in _update_aim recovers it naturally: low-control bots
        visibly walk automatic fire upward.
        """

        kick = recoil_kick_for(int(getattr(runtime.player, "tool", -1)))
        if kick <= 0.0:
            return
        control = float(runtime.profile.recoil_control)
        runtime.motor.pitch_noise -= kick * (1.0 - 0.75 * control)
        runtime.motor.yaw_noise += runtime.rng.gauss(
            0.0, kick * 0.25 * (1.0 - 0.5 * control)
        )

    @staticmethod
    def _set_movement_state(
        runtime: _RuntimeBot,
        values: tuple[bool, bool, bool, bool, bool, bool, bool, bool],
    ) -> None:
        """Apply native movement flags only when a held state changes."""

        # Callers construct this tuple from boolean comparisons/flags already.
        # Re-normalizing eight values for every bot at 60 Hz was measurable in
        # the sustained 12-bot p99 even when the held state did not change.
        if runtime.movement_input == values:
            return
        runtime.movement_input = values
        runtime.player.update_input(*values)

    @staticmethod
    def _set_action_state(
        runtime: _RuntimeBot,
        *,
        primary: bool,
        secondary: bool = False,
        zoom: bool = False,
        hover: bool,
    ) -> None:
        """Update native action flags and the remote held-tool display bit.

        Human clients send ``can_display_weapon`` in ClientData. Peerless bots
        never do, so the director owns that bit for their complete lifetime.
        ``Player.spawn`` resets InputState, therefore the actual display value
        participates in the cache check even if primary/hover did not change.
        """

        display = bool(runtime.player.alive and runtime.player.spawned)
        if (
            runtime.action_primary == primary
            and runtime.action_secondary == secondary
            and runtime.action_zoom == zoom
            and runtime.action_hover == hover
            and bool(runtime.player.input.can_display_weapon) == display
        ):
            return
        runtime.action_primary = primary
        runtime.action_secondary = secondary
        runtime.action_zoom = zoom
        runtime.action_hover = hover
        runtime.player.update_action_input(
            primary,
            secondary,
            zoom=zoom,
            can_display_weapon=display,
            hover=hover,
        )

    def friendly_path_cells(
        self, team: int, *, exclude_owner: int = -1
    ) -> frozenset[tuple[int, int, int]]:
        """Return bounded immediate bot corridors for rare build validation.

        This is intentionally computed on demand instead of every 60 Hz motor
        tick. Construction is rare; locomotion is not.
        """

        cells: set[tuple[int, int, int]] = set()
        for runtime in tuple(self._runtime.values()):
            player = runtime.player
            intent = runtime.intent
            if (
                int(player.id) == int(exclude_owner)
                or int(player.team) != int(team)
                or not player.alive
                or not player.spawned
                or intent is None
            ):
                continue
            dx, dy = (
                float(intent.movement.direction[0]),
                float(intent.movement.direction[1]),
            )
            length = math.hypot(dx, dy)
            if length <= 1e-6:
                continue
            dx, dy = dx / length, dy / length
            body_z = int(math.floor(float(player.z)))
            for distance in (0.75, 1.5, 2.25):
                x = int(math.floor(float(player.x) + dx * distance))
                y = int(math.floor(float(player.y) + dy * distance))
                cells.add((x, y, body_z))
                cells.add((x, y, body_z + 1))
        return frozenset(cells)

    @staticmethod
    def _waypoint_is_live(
        runtime: _RuntimeBot,
        direction,
        affordance: MovementAffordance = MovementAffordance.WALK,
    ) -> bool:
        player = runtime.player
        server = getattr(getattr(player, "connection", None), "server", None)
        world = getattr(server, "world_manager", None)
        if world is None:
            return False
        dx, dy = float(direction[0]), float(direction[1])
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            return True
        dx, dy = dx / length, dy / length
        wading = bool(getattr(player, "wade", False))
        speed = math.hypot(
            float(getattr(player, "vx", 0.0)),
            float(getattr(player, "vy", 0.0)),
        )
        # A close probe catches walls but is too late to arrest native sprint
        # momentum at a water cliff. WorldPlayer velocity is not expressed in
        # blocks/second: a measured 0.35 horizontal velocity needs roughly four
        # blocks to coast to a stop. Sample that whole braking corridor so a
        # one-cell gap cannot hide between the body probe and its endpoint.
        ordinary_walk = affordance in {
            MovementAffordance.WALK,
            MovementAffordance.CROUCH,
        }
        probe_distance = (
            min(5.0, max(1.25, 1.0 + speed * 10.0))
            if ordinary_walk and not wading
            else 0.65
        )
        probe_key = (
            int(round(float(player.x) * 8.0)),
            int(round(float(player.y) * 8.0)),
            int(round(float(player.z) * 8.0)),
            int(round(dx * 100.0)),
            int(round(dy * 100.0)),
            int(getattr(world, "topology_version", 0)),
            wading,
            int(round(probe_distance * 100.0)),
            affordance.value,
        )
        if runtime.waypoint_probe_key == probe_key:
            return runtime.waypoint_probe_result
        shoulder_x, shoulder_y = -dy * 0.45, dx * 0.45

        def probes_at(distance: float):
            center_x = float(player.x) + dx * distance
            center_y = float(player.y) + dy * distance
            return (
                (center_x, center_y),
                (center_x + shoulder_x, center_y + shoulder_y),
                (center_x - shoulder_x, center_y - shoulder_y),
            )

        immediate = probes_at(min(0.65, probe_distance))
        result = all(
            BotDirector._probe_surface_is_live(
                world,
                player,
                probe_x,
                probe_y,
                affordance,
            )
            for probe_x, probe_y in immediate
        )
        if result and ordinary_walk and not wading and probe_distance > 0.65:
            # The immediate full probe owns walls and step height. Farther
            # samples only own water/void braking: comparing their support
            # against the current z would reject a perfectly walkable gradual
            # slope several blocks ahead.
            sample_count = int(math.ceil(probe_distance / 0.5))
            for sample_index in range(2, sample_count + 1):
                distance = min(
                    probe_distance,
                    float(sample_index) * 0.5,
                )
                if any(
                    bool(
                        world.is_water_column(
                            int(math.floor(probe_x)),
                            int(math.floor(probe_y)),
                        )
                    )
                    for probe_x, probe_y in probes_at(distance)
                ):
                    result = False
                    break
        if not result and affordance is MovementAffordance.JUMP:
            body_clear = all(
                not world.clipbox(probe_x, probe_y, float(player.z))
                and not world.clipbox(probe_x, probe_y, float(player.z) + 1.0)
                for probe_x, probe_y in immediate
            )
            if body_clear:
                result = all(
                    BotDirector._probe_surface_is_live(
                        world,
                        player,
                        probe_x,
                        probe_y,
                        affordance,
                    )
                    for probe_x, probe_y in probes_at(2.05)
                )
        runtime.waypoint_probe_key = probe_key
        runtime.waypoint_probe_result = result
        return result

    @staticmethod
    def _live_movement_direction(
        runtime: _RuntimeBot,
        direction,
        affordance: MovementAffordance = MovementAffordance.WALK,
    ) -> tuple[float, float, float]:
        """Return requested locomotion or the nearest body-clear walk vector.

        Worker navigation owns the route. This is only local collision
        steering, analogous to sliding along a wall: special traversal edges
        stay fail-closed because rotating a jump or drop can invalidate its
        authored landing.
        """

        requested = tuple(float(value) for value in direction)
        if BotDirector._waypoint_is_live(runtime, requested, affordance):
            return requested
        if affordance not in {
            MovementAffordance.WALK,
            MovementAffordance.CROUCH,
        }:
            return (0.0, 0.0, 0.0)
        length = math.hypot(requested[0], requested[1])
        if length <= 1e-6:
            return (0.0, 0.0, 0.0)
        dx, dy = requested[0] / length, requested[1] / length
        identity = int(getattr(runtime.player, "id", 0)) + int(
            getattr(runtime, "generation", 0)
        )
        preferred_sign = -1.0 if identity & 1 else 1.0
        for magnitude in _WALK_STEER_ANGLES:
            for sign in (preferred_sign, -preferred_sign):
                angle = magnitude * sign
                cosine, sine = math.cos(angle), math.sin(angle)
                candidate = (
                    (dx * cosine - dy * sine) * length,
                    (dx * sine + dy * cosine) * length,
                    requested[2],
                )
                if BotDirector._waypoint_is_live(runtime, candidate, affordance):
                    return candidate
        return (0.0, 0.0, 0.0)

    @staticmethod
    def _probe_surface_is_live(
        world,
        player,
        probe_x: float,
        probe_y: float,
        affordance: MovementAffordance,
    ) -> bool:
        """Validate one body-width movement probe against current VXL state."""

        # On a two-block JUMP the destination support legitimately intersects
        # the bot's *current* leg-height probe.  Validate the raised landing's
        # two clear body cells below instead; treating that support as a wall
        # erased the worker's correct jump affordance at the final live gate.
        if affordance is not MovementAffordance.JUMP and (
            world.clipbox(probe_x, probe_y, float(player.z))
            or world.clipbox(probe_x, probe_y, float(player.z) + 1.0)
        ):
            return False
        cell_x, cell_y = int(math.floor(probe_x)), int(math.floor(probe_y))
        # A dry plan may never enter the universal water plane. A swimmer's
        # vertical bob is unrelated to support height, however: comparing its
        # current z against the waterbed intermittently rejects every valid
        # waypoint and can freeze it in open water forever.
        wading = bool(getattr(player, "wade", False))
        water_column = bool(world.is_water_column(cell_x, cell_y))
        if not wading and water_column:
            return False
        if wading and water_column:
            return True

        expected_support = int(round(float(player.z) + 2.25))
        climb, drop = {
            MovementAffordance.JUMP: (2, 3),
            MovementAffordance.DROP: (1, 4),
            MovementAffordance.JETPACK: (8, 8),
        }.get(affordance, (1, 1))
        solid = getattr(world, "get_solid", None)
        if callable(solid):
            candidates = range(
                max(2, expected_support - climb),
                min(240, expected_support + drop + 1),
            )
            return any(
                bool(solid(cell_x, cell_y, support_z))
                and not bool(solid(cell_x, cell_y, support_z - 1))
                and not bool(solid(cell_x, cell_y, support_z - 2))
                for support_z in sorted(
                    candidates,
                    key=lambda value: abs(value - expected_support),
                )
            )

        surface_z = int(world.get_height(cell_x, cell_y))
        return expected_support - climb <= surface_z <= expected_support + drop

    @staticmethod
    def _wrap(angle: float) -> float:
        return (angle + math.pi) % (2.0 * math.pi) - math.pi

    def _update_aim(
        self,
        runtime: _RuntimeBot,
        target: tuple[float, float, float],
        dt: float,
        noise_factor: float = 1.0,
    ) -> None:
        player = runtime.player
        motor = runtime.motor
        profile = runtime.profile
        dx = float(target[0]) - float(player.eye_x)
        dy = float(target[1]) - float(player.eye_y)
        dz = float(target[2]) - float(player.eye_z)
        planar = math.hypot(dx, dy)
        if planar <= 1e-6 and abs(dz) <= 1e-6:
            return
        # Ornstein-Uhlenbeck-like correlated error avoids independent jitter.
        # noise_factor < 1 models a human settling onto a tracked target.
        decay = max(0.0, 1.0 - 4.0 * dt)
        noise_scale = (
            profile.aim_noise * math.sqrt(max(dt, 1e-6)) * 2.0 * noise_factor
        )
        motor.yaw_noise = motor.yaw_noise * decay + runtime.rng.gauss(0.0, noise_scale)
        motor.pitch_noise = motor.pitch_noise * decay + runtime.rng.gauss(0.0, noise_scale * 0.6)
        desired_yaw = math.atan2(dy, dx) + motor.yaw_noise
        desired_pitch = math.atan2(dz, max(planar, 1e-6)) + motor.pitch_noise
        motor.yaw, motor.yaw_velocity = self._second_order_axis(
            motor.yaw,
            motor.yaw_velocity,
            desired_yaw,
            profile.turn_speed,
            profile.turn_acceleration,
            dt,
            wrap=True,
        )
        motor.pitch, motor.pitch_velocity = self._second_order_axis(
            motor.pitch,
            motor.pitch_velocity,
            desired_pitch,
            profile.turn_speed * 0.75,
            profile.turn_acceleration * 0.75,
            dt,
            wrap=False,
        )
        motor.pitch = max(-1.35, min(1.35, motor.pitch))
        cos_pitch = math.cos(motor.pitch)
        player.set_orientation_vector(
            math.cos(motor.yaw) * cos_pitch,
            math.sin(motor.yaw) * cos_pitch,
            math.sin(motor.pitch),
        )

    def _second_order_axis(
        self,
        current: float,
        velocity: float,
        target: float,
        max_speed: float,
        max_acceleration: float,
        dt: float,
        *,
        wrap: bool,
    ) -> tuple[float, float]:
        error = self._wrap(target - current) if wrap else target - current
        desired_velocity = max(-max_speed, min(max_speed, error * 7.0))
        velocity_delta = desired_velocity - velocity
        acceleration_step = max_acceleration * max(dt, 0.0)
        velocity += max(-acceleration_step, min(acceleration_step, velocity_delta))
        current += velocity * max(dt, 0.0)
        if wrap:
            current = self._wrap(current)
        return current, velocity

    def _balanced_team(self) -> int:
        counts = {
            team: sum(
                1 for player in self.server.teams[team].players
                if bool(getattr(player, "connection", None))
            )
            for team in _PLAYABLE_TEAMS
        }
        if counts[TEAM1] == counts[TEAM2]:
            return TEAM1 if len(self.bots) % 2 == 0 else TEAM2
        return min(counts, key=counts.get)

    def _update_class_lifetime(self, runtime: _RuntimeBot) -> None:
        """Keep a class for several lives, then stage one normalized change."""

        player = runtime.player
        alive = bool(getattr(player, "alive", False))
        if runtime.was_alive and not alive:
            runtime.class_lives_remaining -= 1
            if runtime.class_lives_remaining <= 0:
                selected_class = self._choose_class(int(player.team), runtime.profile)
                from server.prefabs import allowed_prefabs_for_class

                available = sorted(allowed_prefabs_for_class(selected_class))
                selected_prefabs = _choose_bot_prefabs(
                    available,
                    runtime.rng,
                )
                from server.class_selection import normalize_server_selection
                selection = normalize_server_selection(
                    self.server.config,
                    selected_class,
                    prefabs=selected_prefabs,
                )
                mode = getattr(self.server, "mode", None)
                prepare = getattr(mode, "prepare_join_selection", None)
                if callable(prepare):
                    selection = prepare(int(player.team), selection)
                allows = getattr(mode, "allows_class_selection", None)
                if not callable(allows) or allows(player, selection):
                    player.stage_class_selection(selection)
                runtime.class_lives_remaining = runtime.rng.randint(2, 5)
        runtime.was_alive = alive

    def _choose_class(self, team: int, profile: BotProfile | None = None) -> int:
        """Choose a mode-useful class while avoiding deterministic team stacks."""

        if not _DEFAULT_CLASSES:
            return int(C.DEFAULT_CLASS)
        counts = {class_id: 0 for class_id in _DEFAULT_CLASSES}
        for player in self.server.teams[team].players:
            class_id = int(getattr(player, "class_id", -1))
            if class_id in counts:
                counts[class_id] += 1
        mode = self._mode_id()
        mode_weights: dict[int, float] = {}
        if mode in ("ctf", "cctf"):
            mode_weights = {
                int(C.CLASS_SCOUT): 1.25,
                int(C.CLASS_ENGINEER): 1.40,
                int(C.CLASS_MINER): 1.15,
                int(C.CLASS_MEDIC): 1.25,
            }
        elif mode in ("arena",):
            mode_weights = {
                int(C.CLASS_SOLDIER): 1.25,
                int(C.CLASS_MEDIC): 1.35,
                int(C.CLASS_SPECIALIST): 1.15,
            }
        elif mode in ("zom", "zombie"):
            # Survivor picks favor fortification and sustain; infected
            # variants are forced later by prepare_bot_selection anyway.
            mode_weights = {
                int(C.CLASS_ENGINEER): 1.50,
                int(C.CLASS_MINER): 1.30,
                int(C.CLASS_MEDIC): 1.30,
            }
        else:
            mode_weights = {
                int(C.CLASS_SOLDIER): 1.20,
                int(C.CLASS_SPECIALIST): 1.15,
                int(C.CLASS_MEDIC): 1.15,
            }
        if mode == "tdm" and counts.get(int(C.CLASS_SCOUT), 0) >= 2:
            # Cap the sniper stack: more than two scouts turns TDM passive.
            mode_weights[int(C.CLASS_SCOUT)] = 0.2
        preferred = set(profile.class_preferences if profile is not None else ())
        candidates = list(_DEFAULT_CLASSES)
        weights = []
        for candidate in candidates:
            diversity = 1.0 / (1.0 + counts[candidate] * 0.85)
            preference = 1.35 if candidate in preferred else 1.0
            weights.append(
                diversity * preference * mode_weights.get(candidate, 1.0)
            )
        return int(self._rng.choices(candidates, weights=weights, k=1)[0])

    @staticmethod
    def _select_spawn_weapon(player: "Player") -> None:
        loadout = tuple(
            int(tool) for tool in (getattr(player, "loadout", ()) or ())
        )
        # A Zombie loadout contains no firearm. Falling back to rifle 6 made
        # its first WorldUpdate advertise an item absent from CreatePlayer and
        # left the remote model wrong until the first claw action selected 24.
        # Prefer a firearm for ordinary classes, then a real melee primary,
        # and only use the first normalized item for utility-only classes.
        selected = next((tool for tool in loadout if tool in WEAPON_PROFILES), None)
        if selected is None:
            selected = next((tool for tool in loadout if tool in SPADE_TOOL_IDS), None)
        if selected is None:
            selected = loadout[0] if loadout else DEFAULT_WEAPON_TOOL
        player.set_tool(selected, raw=True)

    def _safe_to_retire(self, player: "Player") -> bool:
        if getattr(player, "pickup_id", None) is not None:
            return False
        mode = self.server.mode
        vips = getattr(mode, "vips", {}) if mode is not None else {}
        if player in getattr(vips, "values", lambda: ())():
            return False
        patient_zero = getattr(mode, "patient_zero_ids", set()) if mode is not None else set()
        if int(player.id) in patient_zero and len(patient_zero) <= 1:
            return False
        # Alive bots are retired only when not under immediate combat pressure.
        runtime = self._runtime.get(int(player.id))
        if bool(getattr(player, "alive", False)) and runtime is not None:
            intent = runtime.intent
            if intent is not None and intent.look is not None and intent.look.visible:
                return False
        return True

    def _mode_id(self) -> str:
        configured = str(getattr(self.server.config, "game_mode", ""))
        if configured:
            return configured.lower()
        mode = getattr(self.server, "mode", None)
        return type(mode).__name__.removesuffix("Mode").lower() if mode else ""

    def _mode_phase(self) -> str:
        """Return a pickle-safe phase name without exposing the mode object."""

        phase = getattr(getattr(self.server, "mode", None), "phase", None)
        if phase is None:
            return ""
        return str(getattr(phase, "name", phase)).lower()
