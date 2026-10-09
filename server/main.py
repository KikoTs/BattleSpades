"""
BattleSpades Main Server
Ace of Spades Protocol 1.0 Battle Builders

Uses ENet for networking with asyncio integration.
"""

import asyncio
from collections import deque
from dataclasses import dataclass
import errno
import inspect
import logging
import socket
import sys
import time
from typing import Any, Dict, Optional, TYPE_CHECKING

from shared.packet import (
    ChatMessage,
    ClockSync,
    CreatePlayer,
    ExistingPlayer,
    FogColor,
    MapSyncChunk,
    MapSyncEnd,
    MapSyncStart,
    PlayerLeft,
    StateData,
    WorldUpdate,
)

from .config import ServerConfig
from .combat_runtime import CombatSystem, get_combat_system
from .corpse_lifecycle import CorpseLifecycle
from . import achievements
from .deployable_actions import DeployableActionService
from .oriented_actions import OrientedActionService
from .construction import ConstructionSafetyService
from .prefab_actions import PrefabActionService
from .game_constants import (
    CHAT_SYSTEM,
    TEAM1,
    TEAM2,
    MAX_HEALTH,
    DEFAULT_BLOCK_HEALTH,
)
from .player import Player, set_movement_authority, INPUT_DELAY_TICKS
from .team import Team
from .world_manager import WorldManager
from .connection import Connection
from .a2s_query import A2SHandler
from .steam_master import SteamMasterService
from .revival_master import RevivalMasterService
from .steam_host import SteamHostService
from .steam_p2p import SteamP2PService
from .debug_parity import DebugParityManager
from .replication import ReplicationService
from .round_lifecycle import RoundLifecycle
from .match import MatchTransitionService
from .simulation_runtime import SimulationRuntime
from .telemetry import TelemetryService
from .terrain_repair import TerrainRepairService
from .world_mutations import WorldMutationService
from .bot_ai.stimuli import BotStimulusBus
from .bot_ai.messages import StimulusKind
# Leaf modules that combat paths import lazily on their first explosion /
# kill. A first-time import does file I/O, which drops the GIL; with the bot
# AI thread busy, each drop costs a whole switch interval (~15.6 ms on
# Windows) and the first blast of a match stalled its tick by 100+ ms.
from . import explosions as _preload_explosions  # noqa: F401
from . import kill_feed as _preload_kill_feed  # noqa: F401

if TYPE_CHECKING:
    import enet

logger = logging.getLogger(__name__)

# Initial health the stock client gives a BlockBuildColored(33) voxel.
BLOCK_COLORED_HEALTH = 3.0
# Retail PROTOCOL_VERSION / shared.steam.game_version().
CLIENT_PROTOCOL_VERSION = 168
ERROR_SERVER_OUT_OF_DATE = 3
ERROR_CLIENT_OUT_OF_DATE = 10


def protocol_version_refusal(data) -> int | None:
    """Retail DISCONNECT reason for a mismatched ENet connect data, else None."""

    try:
        version = int(data)
    except (TypeError, ValueError):
        return ERROR_CLIENT_OUT_OF_DATE
    if version == CLIENT_PROTOCOL_VERSION:
        return None
    if version < CLIENT_PROTOCOL_VERSION:
        return ERROR_CLIENT_OUT_OF_DATE
    return ERROR_SERVER_OUT_OF_DATE

# Native Damage processing changes velocity before Character physics. Across
# six clean retail contacts, the matching authoritative pre-physics state was
# consistently the third ClientData accepted *after* impact; neither
# server.loop_count nor the sparse client loop label was a stable clock. This
# is not a transport ACK. The effect itself is recomputed at that state.
_SNOWBALL_PREDICTION_OBSERVED_FRAMES = 3
# Every blast whose Damage(37) type is a key of the stock
# ``ExplosionDamageManager.damage_functions`` table is predicted the same way:
# ``GameScene.process_packet_damage`` -> ``handle_damage`` dispatches on the
# packet TYPE alone (causer id is not consulted) to ``handle_<x>_damage``,
# which pushes the local character. Recovered by running the stock 32-bit
# ``shared.explosionDamageManager.pyd`` (rules audit 2026-09-27 #9).
STOCK_EXPLOSION_DAMAGE_TYPES = frozenset((
    7, 8, 9, 10, 11, 12, 13, 15, 16, 18, 19, 20, 21, 22, 23, 24,
    30, 33, 37, 38, 39, 40, 41,
))
_BLAST_PREDICTION_OBSERVED_FRAMES = _SNOWBALL_PREDICTION_OBSERVED_FRAMES


class ServerBindError(RuntimeError):
    """Raised when the configured ENet UDP endpoint cannot be acquired."""


def _assert_udp_port_available(port: int) -> None:
    """Fail with an actionable error before ENet obscures a bind failure.

    The vendored pyenet binding raises ``MemoryError`` whenever
    ``enet_host_create`` returns ``NULL``.  That native result also covers
    ordinary socket bind failures, including a second server using the port.
    Probe the endpoint first so operators see the real problem.
    """

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if sys.platform == "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("", int(port)))
    except OSError as exc:
        error_code = getattr(exc, "winerror", None) or exc.errno
        if error_code in (errno.EACCES, errno.EADDRINUSE, 10013, 10048):
            raise ServerBindError(
                f"Cannot start BattleSpades: UDP port {port} is already in "
                "use or reserved. Stop the other server using that port, "
                f"change server.port in config.toml, or launch with --port "
                f"{int(port) + 1}."
            ) from exc
        raise ServerBindError(
            f"Cannot start BattleSpades: UDP port {port} could not be bound: "
            f"{exc}"
        ) from exc
    finally:
        probe.close()


def _create_enet_host(
    enet_module: Any,
    *,
    port: int,
    max_connections: int,
) -> Any:
    """Create the native host while translating pyenet's false MemoryError."""

    _assert_udp_port_available(port)
    address = enet_module.Address(b"", int(port))
    try:
        return enet_module.Host(
            address,
            peerCount=int(max_connections),
            channelLimit=1,
            incomingBandwidth=0,
            outgoingBandwidth=0,
        )
    except MemoryError as exc:
        raise RuntimeError(
            f"ENet could not create its UDP host on port {port}. The native "
            "binding reports both late bind collisions and resource failures "
            "as MemoryError; verify the port is still free and that "
            f"max_connections={max_connections} is valid."
        ) from exc


@dataclass
class _MapCellReplayLease:
    """Stable exact-cell batch retained across a failed reliable send."""

    target_sequence: int
    cells: tuple[tuple[int, int, int], ...]
    next_index: int = 0


@dataclass
class _MapAirReplayLease:
    """Compact pre-snapshot air catch-up retained across input frames."""

    columns: tuple[tuple[int, int, int], ...]
    column_index: int = 0
    current_x: int = 0
    current_y: int = 0
    remaining_mask: int = 0


class BattleSpadesServer:
    """
    Main server class for Battle Builders.
    Manages ENet networking, players, world state, and game logic.
    """
    
    def __init__(
        self,
        config: ServerConfig,
        telemetry: TelemetryService | None = None,
    ):
        self.config = config
        self.running = False
        # ENet peers borrow memory owned by ``self.host``.  Shutdown must make
        # every Python send path inert before dropping that final Host
        # reference, otherwise a delayed coroutine can call into a freed
        # ENetPeer and terminate the process with a native access violation.
        self._stopping = False
        self._stopped = False
        self._stop_lock = asyncio.Lock()
        self._connection_tasks: set[asyncio.Task] = set()
        set_movement_authority(config.movement_authority)
        if config.movement_authority == "client":
            logger.warning(
                "movement_authority=client: echoing client positions "
                "(interim mode until physics parity is reached)"
            )
        
        # ENet host
        self.host: Optional['enet.Host'] = None
        
        # Game state
        self.loop_count = 0
        self.tick_rate = config.tick_rate
        self.tick_interval = 1.0 / self.tick_rate
        
        # Players and connections
        self.players: Dict[int, Player] = {}
        self.connections: Dict[int, Connection] = {}
        # Terrain changes that happen after a joiner's MapSync snapshot but
        # before its first ClientData used to disappear: gameplay broadcasts
        # are deliberately gated during GameScene construction.  Sequence the
        # native block packets and retain them only while a joining connection
        # needs catch-up.
        self._map_mutation_sequence = 0
        self._map_mutation_journal = deque()
        # Exact canonical cells changed after a joining peer's immutable map
        # snapshot. Native damage/collapse packets are excellent live effects,
        # but replaying their semantics later is not deterministic after more
        # terrain edits. This journal has a monotonic per-cell sequence and is
        # replayed from current VXL state at first ClientData.
        self._map_cell_journal = deque()
        self._map_cell_sequence = 0
        self._map_mutation_listener_token = None
        self._next_player_id = 0
        # Ids promised to joining clients via StateData.player_id before
        # their Player object exists (see Connection.send_connection_data).
        self.reserved_player_ids: set = set()
        self.entities: Dict[int, object] = {}
        self.rocket_turrets: Dict[int, object] = {}
        # Placed map entities (crates, intel, ...). self.entities above stays
        # reserved for entities streamed through the 60Hz WorldUpdate; static
        # crates live here and reach clients via StateData (join) + CreateEntity.
        from server.entities.registry import EntityRegistry
        self.entity_registry = EntityRegistry()
        # Live radar stations per owning team (bookkeeping only). The stock
        # client does radar detection itself from the RadarStationEntity, so
        # radar sends no TeamMapVisibility(83).
        self._radar_station_counts = {TEAM1: 0, TEAM2: 0}
        # Plugin system: loaded at startup, fired at the mode-event + tick
        # dispatch points below. Drop a *.py with a BasePlugin subclass in
        # plugins/ and it's auto-discovered.
        from plugins.base_plugin import PluginManager
        self.plugin_manager = PluginManager(self)
        # Prefabs are operator-owned release content. Bind the lazy registry
        # before any action service can resolve a model; never consult a
        # developer-specific client installation as a hidden fallback.
        from server.prefabs import configure_prefab_search_dirs
        # Dedicated variants may add a read-only retail KV6 catalog while the
        # normal release keeps its single portable prefab directory.
        prefab_search_dirs = getattr(
            config, "prefab_search_dirs", (config.prefabs_path,)
        )
        configure_prefab_search_dirs(*prefab_search_dirs)
        # Persistent ban list (bans.json), enforced on connect.
        from server.bans import BanManager
        self.ban_manager = BanManager(config.bans_path)
        # In-flight thrown grenades (server-authoritative blast). Each is a
        # dict: {x,y,z, vx,vy,vz, explode_at, thrower_id}.
        from server.projectiles import ProjectileEngine
        self.projectile_engine = ProjectileEngine()
        from server.rocket_turret import RocketTurretController
        self.rocket_turret_controller = RocketTurretController(self)
        from server.fire import FireController
        self.fire_controller = FireController(self)
        from server.chemical_goo import ChemicalGooController
        self.goo_controller = ChemicalGooController(self)
        from server.voting import VoteManager
        self.vote_manager = VoteManager(self)
        # Mid-match auto-balance and bot difficulty balance (1 Hz, see
        # _run_periodic_services).
        from server.team_balance import TeamBalancer
        self.team_balance = TeamBalancer(self)
        from server.bot_ai.skill_balance import BotSkillBalancer
        self.bot_skill_balance = BotSkillBalancer(self)
        # In-game packets received since the last simulation tick; drained
        # synchronously at the start of each tick so an input that ARRIVED
        # before tick N is guaranteed to be APPLIED at tick N (dispatching
        # via create_task could slip past the tick — input timing became a
        # per-packet race no WorldUpdate stamp offset could compensate).
        self._pending_ingame_packets = deque()
        # connection -> rows it currently holds in _pending_ingame_packets.
        self._pending_ingame_counts: dict = {}
        self._dropped_ingame_packets = 0
        self.bot_stimuli = BotStimulusBus()
        self.telemetry = telemetry or TelemetryService()
        # Compatibility alias for capacity tools and plugins migrated before
        # TelemetryService became the composition boundary.
        self.metrics = self.telemetry.metrics
        # Focused services own the hot runtime contracts. Compatibility
        # delegates keep existing plugins and tests stable during migration.
        self.replication = ReplicationService(self)
        self.round_lifecycle = RoundLifecycle(self)
        self.match_transition = MatchTransitionService(self)
        self.world_mutations = WorldMutationService(self)
        self.simulation_runtime = SimulationRuntime(self)
        # Game-logic events (kills, deaths, spawns, block edits, team changes)
        # queued SYNCHRONOUSLY from the sim/combat/packet paths and drained
        # once per tick in _game_loop. This is the SINGLE place mode hooks
        # fire — never asyncio.create_task from a sync path (it would slip
        # past the tick and reintroduce the input-timing race main.py warns
        # about above). (name, args) tuples.
        self._mode_events = deque()

        # Teams
        self.teams = {
            TEAM1: Team(TEAM1, config.team1_name, config.team1_color),
            TEAM2: Team(TEAM2, config.team2_name, config.team2_color),
        }
        
        # World
        self.world_manager = WorldManager(config)
        self._bind_world_mutation_journal()
        # /fog is a per-map live override. A full map/mode rollover clears it
        # so the replacement scene receives its authored atmosphere again.
        self.fog_color_override = None
        from server.map_resources import MapResourceService
        self.map_resources = MapResourceService(self)
        self.terrain_repair = TerrainRepairService(self)
        self.corpse_lifecycle = CorpseLifecycle(self)
        self.combat = CombatSystem(self)
        self.construction = ConstructionSafetyService(self)
        self.prefab_actions = PrefabActionService(self)
        self.deployable_actions = DeployableActionService(self)
        self.oriented_actions = OrientedActionService(self)
        self.debug_parity = DebugParityManager(self)
        # Inert until start(): hooks are no-ops and no file is opened.
        self.achievements = achievements.AchievementEngine(self)
        
        # A2S Query handler for Steam browser and LAN discovery
        self.a2s_handler = A2SHandler(self)
        # Optional legacy master registration owns its 32-bit DLL helper and
        # callback cadence outside SimulationRuntime.
        self.steam_master = SteamMasterService(self)
        self.revival_master = RevivalMasterService(self)
        self.steam_p2p = SteamP2PService(self)
        self.steam_host = SteamHostService(self)
        
        # Game mode
        self.mode = None
        # Dev bots (server-side AI players) — created in start() if configured.
        self.bots = None
    
    def get_player_by_name(self, name: str) -> Optional[Player]:
        """Find a player by name (case-insensitive partial match)."""
        name_lower = name.lower()
        for player in self.players.values():
            if player.name.lower().startswith(name_lower):
                return player
        return None
    
    def get_next_player_id(self) -> int:
        """Get the next available player ID (skips reserved ids promised to
        clients that are still mid-join)."""
        for i in range(self.config.max_players):
            if i not in self.players and i not in self.reserved_player_ids:
                return i
        return -1

    async def _load_plugins(self) -> None:
        """Discover BasePlugin subclasses in the configured runtime directory.
        Top-level public Python files are considered. Failures are logged, never
        fatal — a bad plugin can't take the server down."""
        from pathlib import Path
        from server.plugin_loader import load_external_plugins

        if not bool(getattr(self.config, "plugins_enabled", True)):
            logger.info("Plugin loading disabled by configuration")
            return
        loaded = await load_external_plugins(
            self.plugin_manager,
            Path(self.config.plugins_path),
            allowlist=getattr(self.config, "plugin_allowlist", ()),
            denylist=getattr(self.config, "plugin_denylist", ()),
        )
        if loaded:
            logger.info("Loaded %d plugin(s)", loaded)

    def queue_mode_event(self, name: str, *args) -> None:
        """Queue a game-logic event for the active mode. Drained once per tick
        in _game_loop (after on_tick). Safe to call from synchronous code
        (Player.die, combat, packet handlers) — never schedules a task."""
        if len(self._mode_events) >= int(self.config.mode_event_queue_limit):
            self.metrics.dropped_mode_events += 1
            return
        self._mode_events.append((name, args))

    # Events whose handler would give the departing player new mode state.
    _LEAVER_REACQUIRE_EVENTS = frozenset({
        "on_player_spawn",
        "on_player_team_change",
    })
    # Already-happened scoring events involving the departing player.
    _LEAVER_SETTLE_EVENTS = frozenset({
        "on_player_death",
        "on_player_kill",
    })

    def _run_player_leave_hooks(self, player) -> None:
        """Run mode + plugin ``on_player_leave`` for a disconnect, now.

        Called from the synchronous ENet disconnect path while ``player`` is
        still in ``players`` and its team, before PlayerLeft. The network
        loop only runs between simulation steps, so this is still outside
        any tick.

        Queued events are reconciled first:

        - events that would re-acquire mode state for the departing player
          (``_LEAVER_REACQUIRE_EVENTS``: spawn, team change) are discarded;
          after the leave hook released that state they would re-arm a
          departed id;
        - the departing player's pending ``on_player_death`` /
          ``on_player_kill`` (as victim or killer) are settled NOW, in queue
          order, before the leave hook. They record things that already
          happened: a kill in the same tick as the disconnect still earns
          the TDM team point, and a VIP killed just before quitting still
          pays the killer's bonus instead of the leave hook recording a
          killer-less VIP death first;
        - every other event (other players' kills, block events) stays
          queued untouched.
        """
        if self._mode_events:
            settle_now = []
            kept = []
            for name, args in self._mode_events:
                involves = bool(args) and any(arg is player for arg in args[:2])
                if involves and name in self._LEAVER_REACQUIRE_EVENTS and args[0] is player:
                    continue
                if involves and name in self._LEAVER_SETTLE_EVENTS:
                    settle_now.append((name, args))
                    continue
                kept.append((name, args))
            # In place: the drain loop holds a reference to this deque.
            self._mode_events.clear()
            self._mode_events.extend(kept)
            mode = self.mode
            plugins = getattr(self, "plugin_manager", None)
            call_event = getattr(plugins, "call_event", None)
            for name, args in settle_now:
                handler = getattr(mode, name, None) if mode is not None else None
                if callable(handler):
                    self._drive_hook_now(f"mode {name} (leaver)", handler, *args)
                if callable(call_event):
                    self._drive_hook_now(
                        f"plugin {name} (leaver)", call_event, name, *args
                    )
        mode = self.mode
        if mode is not None:
            hook = getattr(mode, "on_player_leave", None)
            if callable(hook):
                self._drive_hook_now("mode on_player_leave", hook, player)
        plugins = getattr(self, "plugin_manager", None)
        call_event = getattr(plugins, "call_event", None)
        if callable(call_event):
            self._drive_hook_now(
                "plugin on_player_leave", call_event, "on_player_leave", player
            )

    def _drive_hook_now(self, label: str, hook, *args) -> None:
        """Run a (possibly async) hook to completion synchronously.

        Mode leave hooks only emit packets and mutate state, so they finish
        without suspending. If one does suspend, its remainder continues on
        the event loop as a retained task instead of blocking the network
        loop; everything before its first await has already run in order.
        """
        try:
            result = hook(*args)
        except Exception:
            logger.exception("%s failed", label)
            return
        if not inspect.isawaitable(result):
            return
        iterator = result.__await__()
        try:
            pending = iterator.send(None)
        except StopIteration:
            return
        except Exception:
            logger.exception("%s failed", label)
            return

        async def _finish(pending=pending):
            while True:
                if pending is None:
                    await asyncio.sleep(0)
                else:
                    await asyncio.wait([pending])
                try:
                    pending = iterator.send(None)
                except StopIteration:
                    return
                except Exception:
                    logger.exception("%s failed", label)
                    return

        task = asyncio.ensure_future(_finish())
        self._connection_tasks.add(task)
        task.add_done_callback(self._connection_tasks.discard)

    async def _run_periodic_services(self) -> None:
        """Once-per-second gameplay services that need the fixed tick.

        Runs inside the simulation step right before respawns, so a dead
        player the team balancer moves respawns on the new side in this same
        tick. Each service is isolated: one failure never skips the others
        or the respawn pass.
        """
        tick_rate = max(1, int(getattr(self, "tick_rate", 60) or 60))
        if int(getattr(self, "loop_count", 0)) % tick_rate != 0:
            return
        now = time.monotonic()
        balancer = getattr(self, "team_balance", None)
        if balancer is not None:
            try:
                await balancer.tick(now)
            except Exception:
                logger.exception("team balance tick failed")
        skill_balance = getattr(self, "bot_skill_balance", None)
        if skill_balance is not None:
            try:
                skill_balance.update(now)
            except Exception:
                logger.exception("bot skill balance update failed")
        try:
            from server import anticheat_report
        except ImportError:
            anticheat_report = None
        report_tick = getattr(anticheat_report, "tick", None)
        if callable(report_tick):
            try:
                report_tick(self, now)
            except Exception:
                logger.exception("anticheat report tick failed")

    async def _process_respawns(self) -> None:
        """Compatibility delegate to the round lifecycle service."""
        await self._run_periodic_services()
        lifecycle = getattr(self, "round_lifecycle", None)
        if lifecycle is None:
            lifecycle = RoundLifecycle(self)
            self.round_lifecycle = lifecycle
        await lifecycle.process_respawns()

    def respawn_player(self, player) -> None:
        """Compatibility delegate shared by death and round restarts."""
        lifecycle = getattr(self, "round_lifecycle", None)
        if lifecycle is None:
            lifecycle = RoundLifecycle(self)
            self.round_lifecycle = lifecycle
        lifecycle.respawn_player(player)

    def reset_round_runtime(self) -> None:
        """Compatibility delegate for same-map transient cleanup."""
        lifecycle = getattr(self, "round_lifecycle", None)
        if lifecycle is None:
            lifecycle = RoundLifecycle(self)
            self.round_lifecycle = lifecycle
        lifecycle.reset_round_runtime()

    def spawn_grenade(self, player, packet) -> bool:
        """A player used an oriented item (grenade family, RPG/RPG2 rocket,
        drill, snowball, sticky/chemical). Register the server-authoritative
        projectile + rebroadcast the packet so every other client renders and
        simulates it locally (arc/flight + explosion FX + sound)."""
        import shared.constants as C
        from server.projectiles import PROJECTILE_SPECS
        tool = int(getattr(packet, "tool", 0))
        if tool not in PROJECTILE_SPECS:
            return False  # deployables ride their dedicated Place* packets

        pos = getattr(packet, "position", None)
        vel = getattr(packet, "velocity", None)
        if not pos or not vel:
            return False
        # Reject NaN/inf (a bad float can wedge the sim).
        vals = list(pos) + list(vel) + [getattr(packet, "value", 0.0)]
        if any(v != v or abs(v) > 1e6 for v in vals):
            return False

        fuse = max(0.0, min(float(getattr(packet, "value", 3.0)), 10.0))

        p = self.projectile_engine.spawn(tool, pos, vel, fuse, player.id)
        if p is None:
            return False

        entity_color = None
        if tool in {
            int(getattr(C, "SNOWBLOWER_TOOL", 29)),
            int(getattr(C, "UGC_SNOWBLOWER_TOOL", 48)),
        }:
            # The retail weapon is named Block Cannon in the final strings.
            # It consumes one ordinary block per shot, and the projectile must
            # retain the selected palette colour even if the player changes it
            # before impact.
            p.block_color = int(getattr(player, "block_color", 0)) & 0xFFFFFF
            p.source_loop = max(0, int(getattr(packet, "loop_count", 0)))
            entity_color = (
                (p.block_color >> 16) & 0xFF,
                (p.block_color >> 8) & 0xFF,
                p.block_color & 0xFF,
            )

        if p.spec.entity_type:
            # These client send_* methods do not create a local world object;
            # the throw/launcher code only computes velocity and calls the
            # GameScene network sender. The server therefore creates exactly
            # one entity for every client, including the shooter:
            # spawn a CreateEntity of the right ENTITY type carrying the initial
            # pos+velocity so EVERY client renders + simulates the projectile,
            # and DestroyEntity on explosion plays the blast FX (see _explode).
            from server.connection import internal_team_to_wire
            state = internal_team_to_wire(player.team)
            ent = self.entity_registry.place(
                int(p.spec.entity_type),
                float(pos[0]), float(pos[1]), float(pos[2]),
                state=state, kind="projectile", player_id=player.id,
                color=entity_color,
                vel=(float(vel[0]), float(vel[1]), float(vel[2])),
                radius=0.02,
                fuse=(
                    float(getattr(C, "STICKY_GRENADE_STICK_FUSE", 5.0))
                    if tool == int(getattr(C, "STICKY_GRENADE_TOOL", 57))
                    else fuse
                ),
            )
            p.entity_id = ent.entity_id
            self.broadcast_create_entity(ent)
        else:
            # Grenade family: the client renders a thrown grenade from the
            # rebroadcast UseOrientedItem. Send it to everyone EXCEPT the
            # thrower (whose client already simulates its own throw).
            from shared.packet import UseOrientedItem
            out = UseOrientedItem()
            out.loop_count = self.loop_count
            out.player_id = player.id
            out.tool = tool
            out.value = fuse
            out.position = tuple(float(v) for v in pos)
            out.velocity = tuple(float(v) for v in vel)
            data = bytes(out.generate())
            for conn in list(self.connections.values()):
                if not conn.in_game or conn.player is None or conn.player.id == player.id:
                    continue
                try:
                    conn.send(data)
                except Exception:
                    logger.debug("projectile rebroadcast failed", exc_info=True)

        logger.info("PROJECTILE %s by %s tool=%d pos=(%.1f,%.1f,%.1f) fuse=%.2f eid=%s",
                    p.spec.name, player.name, tool, pos[0], pos[1], pos[2], fuse, p.entity_id)
        return True

    # Projectile physics lives in server/projectiles.py (the grenade math is
    # the verified port of the compiled client's mover sub_10011E90; rockets/
    # drill/snowball fly per the client's extracted flight constants).

    def _update_grenades(self, dt: float) -> None:
        """Advance all in-flight projectiles; apply any explosions."""
        events = self.projectile_engine.update(
            dt, self.world_manager, players=tuple(self.players.values())
        )
        # Sticky 34 -> 35 swaps first, so a blast below names the live id.
        from server.entities import attachments
        attachments.publish_sticky_events(self)
        attachments.sweep_riot_shields(self)
        from server.projectiles import DrillContact, ProjectileDeployment
        for event in events:
            if isinstance(event, DrillContact):
                self._apply_drill_contact(event)
            elif isinstance(event, ProjectileDeployment):
                self._deploy_launched_mine(event)
            else:
                self._explode_projectile(event)

    def _apply_drill_contact(self, event) -> None:
        """Apply one Drill bore with the client's exact footprint.

        A live type-10 packet is compact and drives the retail Drill sound,
        particles, and the radius-3 BlockManager footprint (seeded; see
        block_damage_model) the server has just applied per cell.  It requires a
        still-live projectile entity, however, so reconnect catch-up records
        type-6 exact cells instead.  If the entity vanished unexpectedly, the
        live path also falls back to those exact cells rather than triggering
        the native ``Drill entity ID not valid`` abort.
        """
        import shared.constants as C
        from server.projectiles import drill_contact_cells
        from shared.packet import Damage

        owner = self.players.get(event.projectile.thrower_id)
        if owner is None:
            return

        combat = get_combat_system(self)
        # Retail handle_drill_damage = radius-3 footprint with falloff and a
        # seeded random extra (block_damage_model): 20 damage bores the
        # 81-cell core through map voxels, while 9-health built blocks near
        # the rim may survive exactly as they do on every client.
        seed, amount, destroyed, damaged = combat.apply_native_terrain_damage(
            owner,
            tuple(float(value) for value in event.block),
            int(C.DRILL_DAMAGE),
            float(getattr(C, "DRILL_DRILLING_BLOCK_DAMAGE", 20.0)),
        )
        if not destroyed and not damaged:
            return

        raw_entity_id = getattr(event.projectile, "entity_id", None)
        live_entity = (
            self.entity_registry.get(int(raw_entity_id))
            if raw_entity_id is not None
            else None
        )
        if live_entity is None:
            for data in combat._exact_outcome_packets(owner, destroyed, damaged):
                self.broadcast(data, reliable=True, record_mutation=False)
            if destroyed:
                combat.record_exact_block_destroy_catchup(
                    owner, destroyed, causer_id=int(owner.id)
                )
                with achievements.block_cause(self, owner, int(C.DRILL_KILL)):
                    combat._collapse_unsupported(owner, destroyed)
            return

        packet = Damage()
        packet.player_id = int(owner.id)
        packet.type = int(C.DRILL_DAMAGE)
        packet.damage = amount
        packet.face = 0
        packet.chunk_check = 1
        packet.seed = int(seed)
        # Entity id 0 is valid; never use truthiness as the sentinel here.
        packet.causer_id = int(raw_entity_id)
        packet.position = tuple(float(value) for value in event.block)
        self.broadcast(
            bytes(packet.generate()),
            reliable=True,
            record_mutation=False,
        )

        # A joiner's MapSync snapshot may predate this bore but its replay may
        # occur after the projectile is gone. Journal stable exact cells only.
        if destroyed:
            combat.record_exact_block_destroy_catchup(
                owner,
                destroyed,
                causer_id=int(owner.id),
            )
            # The bore is not a blast: name the drill for block achievements.
            with achievements.block_cause(self, owner, int(C.DRILL_KILL)):
                combat._collapse_unsupported(owner, destroyed)

    def _deploy_launched_mine(self, event) -> None:
        """Turn a Mine Launcher projectile's terrain contact into an armed,
        replicated landmine. It uses the same behavior and stock constants as
        a hand-placed Scout mine."""
        owner = self.players.get(event.thrower_id)
        if owner is None:
            return
        import shared.constants as C
        from server.connection import internal_team_to_wire
        from server.entities.behaviors import ProximityMineBehavior

        # Flying ProjectileMine (37) and armed Landmine (9) are distinct
        # retail objects. Remove the first before publishing the second.
        flight_id = getattr(event, "entity_id", None)
        if flight_id is not None:
            flight_id = int(flight_id)
            if self.entity_registry.remove(flight_id) is not None:
                self.broadcast_destroy_entity(flight_id)

        support_cell = getattr(event, "support_cell", None)
        if (
            support_cell is None
            or not self.world_manager.get_solid(*support_cell)
        ):
            # Bounds expiry and stale contacts are not placements.  The old
            # path converted either into a permanent unsupported landmine.
            logger.debug(
                "MINE LAUNCHER discarded unsupported deployment for %s at %s",
                owner.name,
                support_cell,
            )
            return

        behavior = ProximityMineBehavior(
            owner.id,
            owner.team,
            damage=float(getattr(C, "LANDMINE_EXPLOSION_DAMAGE", 100.0)),
            block_damage=float(getattr(C, "LANDMINE_EXPLOSION_BLOCK_DAMAGE", 15.0)),
            crater_radius=1,
            kill_type=int(getattr(C.KILL, "MINE_KILL", 35)),
            trigger_radius=float(getattr(C, "LANDMINE_DETECTION_RANGE", 2.5)),
            arm_delay=float(getattr(C, "LANDMINE_ACTIVATION_TIMER", 4.0)),
            blast_radius=float(getattr(C, "LANDMINE_EXPLOSION_RADIUS", 3.0)),
            force_destroy=False,
            detection_layers=int(getattr(C, "LANDMINE_DETECTION_LAYERS", 3)),
            health=float(getattr(C, "LANDMINE_HEALTH", 1.0)),
            vertical_offset=float(getattr(
                C,
                "LANDMINE_EXPLOSION_AND_DETECTION_VERTICAL_OFFSET",
                -0.5,
            )),
            damage_type=int(getattr(C, "MINE_LAUNCHER_DAMAGE", 40)),
        )
        ent = self.entity_registry.place(
            int(getattr(C, "LANDMINE_ENTITY", 9)),
            event.x, event.y, event.z,
            state=internal_team_to_wire(owner.team),
            kind="deployable", player_id=owner.id, behavior=behavior,
            support_cell=support_cell,
        )
        self.broadcast_create_entity(ent)
        logger.info(
            "MINE LAUNCHER deployed mine id=%d for %s at (%.1f,%.1f,%.1f)",
            ent.entity_id, owner.name, event.x, event.y, event.z,
        )

    def _explode_projectile(self, ex) -> None:
        """Detonate a projectile: crater a 3x3x3 block cube (damage-gated for
        weak warheads) and damage nearby players with distance falloff +
        line-of-sight. Grenade-family numbers match the live-verified blast."""
        gx, gy, gz = ex.x, ex.y, ex.z
        thrower = self.players.get(ex.thrower_id)
        logger.info("%s explode at (%.1f,%.1f,%.1f)", ex.spec.name.upper(), gx, gy, gz)

        # Remove the flying entity on all clients (plays the explosion FX for
        # rocket/drill/snowball/molotov, which the client would otherwise fly
        # forever — stop_on_collision is False on the client's projectile).
        raw_entity_id = getattr(ex, "entity_id", None)
        if raw_entity_id is not None:
            eid = int(raw_entity_id)
            live_entity = self.entity_registry.get(eid)
        else:
            live_entity = None
        prediction_sent = False
        if raw_entity_id is not None and live_entity is None:
            # Cleanup/disconnect may have removed the visual before an already
            # queued engine event reached this method. Never emit Damage with
            # a causer id the retail client can no longer resolve.
            logger.debug(
                "Discarding stale %s explosion for missing entity %s",
                ex.spec.name,
                raw_entity_id,
            )
            return
        if live_entity is not None:
            if ex.spec.name == "snowball":
                self._place_block_cannon_impact(ex, thrower)

            # DestroyEntity only removes the Snowball visual. Stock retail
            # applies its predicted blast impulse from Damage(37), so publish
            # that event while ``causer_id`` still names a live entity. Keep
            # this Snowball-only until every crater-producing Damage type has
            # been verified; generalising it can duplicate terrain mutations
            # in the native BlockManager.
            if ex.spec.name == "snowball":
                from shared.packet import Damage

                prediction = Damage()
                prediction.player_id = int(ex.thrower_id)
                prediction.type = int(ex.spec.damage_type)
                prediction.damage = float(ex.block_damage)
                prediction.face = 0
                prediction.chunk_check = 0
                prediction.seed = 0
                prediction.causer_id = eid
                prediction.position = (float(gx), float(gy), float(gz))
                self.broadcast(
                    bytes(prediction.generate()),
                    reliable=True,
                    record_mutation=False,
                )
                prediction_sent = True
            if self.entity_registry.remove(eid) is not None:
                self.broadcast_destroy_entity(eid)

        if ex.spec.name == "chemical_bomb":
            # Retail has no Chemical Bomb blast: the stock shared
            # ExplosionDamageManager has a handler for every other late
            # explosive (GL grenade, sticky, radar, mine, C4) but none for
            # it, and its Damage type 43 is single-block (no crater). The
            # bomb's whole effect is the goo it leaves behind.
            self.goo_controller.splash(gx, gy, gz, thrower)
            return

        # Projectiles that don't self-destroy blocks (RPG2, block_damage 2)
        # ACCUMULATE damage; grenade-family + strong warheads destroy outright.
        force_destroy = ex.spec.behavior != "contact"
        achievements.projectile_exploding(self, ex, thrower)
        self._apply_blast(gx, gy, gz, ex.damage, ex.block_damage,
                          ex.spec.kill_type, thrower, crater_radius=1,
                          force_destroy=force_destroy,
                          terrain_damage_type=int(ex.spec.damage_type),
                          blast_radius=float(ex.blast_radius),
                          knockback_min=float(ex.knockback_min),
                          knockback_max=float(ex.knockback_max),
                          self_knockback_min=ex.self_knockback_min,
                          self_knockback_max=ex.self_knockback_max,
                          prediction_frame_delay=(
                              _SNOWBALL_PREDICTION_OBSERVED_FRAMES
                              if prediction_sent
                              else None
                          ))
        if ex.spec.name == "molotov":
            self.fire_controller.ignite_impact(gx, gy, gz, thrower)

    def _apply_blast_terrain(self, gx, gy, gz, block_damage, thrower, *,
                             crater_radius: int = 1,
                             damage_type: int | None = None,
                             causer_entity_id: int | None = None) -> bool:
        """Apply one explosion's terrain damage with per-cell health.

        Returns True when ONE native expanding ``Damage(37)`` of
        ``damage_type`` was broadcast. For a stock explosion type that packet
        is also each stock client's knockback prediction, so it is sent even
        when the footprint holds no solid cell (every client derives the same
        empty footprint and changes no terrain).
        """

        import shared.constants as C
        from server import block_damage_model

        combat = get_combat_system(self)
        # WEAPON_DAMAGE (6) is the exact single-cell type; a projectile spec
        # that carries it (chemical bomb, type not in the catalog) has no
        # measured blast footprint and keeps the cube below.
        if (
            damage_type is not None
            and int(damage_type) != int(C.WEAPON_DAMAGE)
            and block_damage_model.is_native(damage_type)
        ):
            seed, amount, destroyed, damaged = (
                combat.apply_native_terrain_damage(
                    thrower, (gx, gy, gz), int(damage_type), block_damage
                )
            )
            if not destroyed and not damaged and (
                int(damage_type) not in STOCK_EXPLOSION_DAMAGE_TYPES
            ):
                return False
            if causer_entity_id is not None:
                combat.broadcast_native_radius_destroy(
                    thrower,
                    (float(gx), float(gy), float(gz)),
                    destroyed,
                    damage=amount,
                    damage_type=int(damage_type),
                    causer_entity_id=int(causer_entity_id),
                    seed=seed,
                    damaged=damaged,
                )
            else:
                combat.broadcast_native_terrain_damage(
                    thrower,
                    (float(gx), float(gy), float(gz)),
                    destroyed,
                    damage=amount,
                    damage_type=int(damage_type),
                    seed=seed,
                )
            return True

        # No retail footprint known for this source: damage the crater cube
        # cell by cell with exact type-6 packets, still through per-cell
        # health so built blocks survive exactly as long as on clients.
        import math

        bx, by, bz = (int(math.floor(float(v) + 0.5)) for v in (gx, gy, gz))
        r = max(1, int(crater_radius))
        for block in [
            (ax, ay, az)
            for ax in range(bx - r, bx + r + 1)
            for ay in range(by - r, by + r + 1)
            for az in range(bz - r, bz + r + 1)
        ]:
            if self.world_manager.get_solid(*block):
                combat._apply_block_damage(thrower, block, block_damage)
        return False

    def _place_block_cannon_impact(self, ex, thrower) -> bool:
        """Commit one Block Cannon voxel at a terrain impact.

        The Snowball ``Damage`` event is only blast prediction; damage type 20
        is deliberately absent from the native BlockManager's terrain-damage
        table.  The server must therefore add the last free voxel itself and
        publish an explicit-colour packet.  Broadcasting through the normal
        mutation path also journals the build for clients whose VXL snapshot
        was already in flight.
        """
        if thrower is None or getattr(ex, "contact_block", None) is None:
            return False

        position = (int(ex.x), int(ex.y), int(ex.z))
        color = getattr(ex, "block_color", None)
        if color is None:
            color = int(getattr(thrower, "block_color", 0)) & 0xFFFFFF
        else:
            color = int(color) & 0xFFFFFF

        combat = get_combat_system(self)
        world = self.world_manager
        if not world.can_build(*position) or not combat._block_supported(*position):
            return False
        # Every stock client stores a BlockBuildColored(33) voxel at 3.0
        # health (live 2026-09-26); record the same so all break it together.
        if not world.set_block(*position, True, color, health=BLOCK_COLORED_HEALTH):
            return False

        from shared.packet import BlockBuildColored

        packet = BlockBuildColored()
        source_loop = getattr(ex, "source_loop", None)
        packet.loop_count = (
            max(0, int(source_loop))
            if source_loop is not None
            else max(0, int(self.loop_count))
        )
        packet.player_id = int(thrower.id)
        packet.x, packet.y, packet.z = position
        packet.color = color
        self.broadcast(bytes(packet.generate()), reliable=True)
        logger.info(
            "BLOCK CANNON built (%d,%d,%d) color=%06X for %s",
            position[0], position[1], position[2], color, thrower.name,
        )
        return True

    @achievements.blast_scope
    def _apply_blast(self, gx, gy, gz, damage, block_damage, kill_type, thrower,
                     crater_radius: int = 1, force_destroy: bool = True,
                     blast_radius: float = 16.0, knockback_min: float = 0.0,
                     knockback_max: float = 0.0,
                     self_knockback_min=None, self_knockback_max=None,
                     prediction_frame_delay: int | None = None,
                     native_damage_type: int | None = None,
                     causer_entity_id: int | None = None,
                     ignore_player_los: bool = False,
                     terrain_damage_type: int | None = None) -> None:
        """Shared explosion: damage terrain and nearby players.

        Terrain follows the retail client's own BlockManager footprint for
        the explosive's damage type (:mod:`server.block_damage_model`, live
        fitted 2026-09-26): every cell's damage is applied to the canonical
        map with its own health (9 for built blocks, 5 for map voxels) and
        ONE native Damage packet with the same seed makes every client apply
        the identical per-cell damage.  ``native_damage_type`` (deployables,
        which also carry ``causer_entity_id``) or ``terrain_damage_type``
        (projectiles) selects the footprint; an explosion without a known
        footprint damages the ``crater_radius`` cube cell by cell through the
        same per-cell health.  ``force_destroy`` is retained for callers but
        no longer bypasses block health.  Players take the live-verified
        falloff."""
        stimuli = getattr(self, "bot_stimuli", None)
        if stimuli is not None:
            stimuli.publish(
                StimulusKind.EXPLOSION,
                (float(gx), float(gy), float(gz)),
                source_id=int(getattr(thrower, "id", -1)),
                team=int(getattr(thrower, "team", -1)),
                radius=max(48.0, float(blast_radius) * 5.0),
                lifetime=2.0,
            )
        terrain_packet_sent = False
        if getattr(self.config, "build_damage", True) and block_damage > 0.0:
            terrain_packet_sent = self._apply_blast_terrain(
                gx, gy, gz, block_damage, thrower,
                crater_radius=crater_radius,
                damage_type=(
                    native_damage_type
                    if native_damage_type is not None
                    else terrain_damage_type
                ),
                causer_entity_id=(
                    causer_entity_id if native_damage_type is not None else None
                ),
            )
        packet_type = (
            native_damage_type if native_damage_type is not None
            else terrain_damage_type
        )
        if (
            prediction_frame_delay is None
            and terrain_packet_sent
            and packet_type is not None
            and int(packet_type) in STOCK_EXPLOSION_DAMAGE_TYPES
        ):
            # Stock clients push their own character while processing that
            # Damage(37); apply the authoritative push on the matching
            # accepted input frame instead of at server-side contact, so the
            # owner is not corrected by an impulse it has not predicted yet.
            prediction_frame_delay = _BLAST_PREDICTION_OBSERVED_FRAMES

        # Player blast damage: the stock shared ExplosionDamageManager
        # (server/weapons_retail.py, docs/WEAPONS_RETAIL.md). A blast whose
        # kill type + warhead damage matches a stock handler uses that
        # handler's radius/knockback/classic flag, so every caller gets the
        # retail curve even if it passed a different radius.
        from server import weapons_retail as retail
        from server.explosions import explosion_impulse

        stock = retail.retail_explosion_for(kill_type, damage)
        classic = False
        if stock is not None:
            blast_radius = float(stock.radius)
            knockback_min = float(stock.knockback_min)
            knockback_max = float(stock.knockback_max)
            classic = bool(stock.classic)
        origin = (float(gx), float(gy), float(gz))
        # Sight-ray occlusion: the world raycast reproduces the stock
        # 10%..110% hitscan_accurate ray exactly; a server without a world
        # (unit fixtures) falls back to its segment LOS test.
        segment_blocked = None
        if not ignore_player_los:
            world_raycast = getattr(
                getattr(self, "world_manager", None), "raycast", None
            )
            if callable(world_raycast):
                segment_blocked = retail.raycast_segment_blocker(world_raycast)
            else:
                def segment_blocked(start, end):
                    return bool(self._blocked_los(
                        start[0], start[1], start[2], end[0], end[1], end[2]
                    ))
        for target in list(self.players.values()):
            if not target.alive or not target.spawned:
                continue
            crouched = bool(getattr(
                target, "hitbox_crouched",
                getattr(getattr(target, "input", None), "crouch", False),
            ))
            raw_position = target.position
            position = (float(raw_position[0]), float(raw_position[1]),
                        float(raw_position[2]))
            falloff = retail.explosion_falloff(
                origin, position, blast_radius,
                retail.BODY_OFFSET_CROUCHING if crouched
                else retail.BODY_OFFSET_STANDING,
            )
            if falloff <= 0.0:
                continue
            # Three stock sight rays (head/torso/legs, weights .5/.3/.2).
            los_fraction = retail.explosion_los_fraction(
                segment_blocked, origin, position, crouched
            )
            if los_fraction <= 0.0:
                continue
            target_knockback_min = float(knockback_min)
            target_knockback_max = float(knockback_max)
            if target is thrower:
                if self_knockback_min is not None:
                    target_knockback_min = float(self_knockback_min)
                if self_knockback_max is not None:
                    target_knockback_max = float(self_knockback_max)
            # The stock impulse magnitude is scaled by the LOS fraction.
            target_knockback_min, target_knockback_max = retail.scale_knockback(
                target_knockback_min, target_knockback_max, los_fraction
            )
            impulse_preview = explosion_impulse(
                origin, target.position, blast_radius,
                target_knockback_min, target_knockback_max,
                crouched=crouched,
            )
            if target is thrower and impulse_preview is not None:
                note_push = getattr(target, "note_own_blast_push", None)
                if callable(note_push):
                    # ROCKET_JUMP_FALL_DAMAGE_MULTIPLIER for the landing.
                    note_push(int(kill_type))
            queue_explosion = getattr(target, "queue_explosion_impulse", None)
            target_input_sequence = None
            deferred_prediction = (
                prediction_frame_delay is not None
                and callable(queue_explosion)
                and impulse_preview is not None
            )
            if deferred_prediction:
                target_input_sequence = queue_explosion(
                    prediction_frame_delay,
                    (gx, gy, gz),
                    blast_radius,
                    target_knockback_min,
                    target_knockback_max,
                )

            if impulse_preview is not None:
                if bool(getattr(self.config, "movement_debug_capture", False)):
                    target.last_explosion_impulse_debug = {
                        "server_loop": int(self.loop_count),
                        "prediction_frame_delay": prediction_frame_delay,
                        "target_input_sequence": target_input_sequence,
                        "last_applied_input_loop": target.last_applied_input_loop,
                        "queued_input_loops": tuple(sorted(target.input_history)),
                        "position_before": tuple(target.position),
                        "velocity_before": tuple(target.velocity),
                        "impulse_preview": tuple(impulse_preview),
                        "origin": (float(gx), float(gy), float(gz)),
                    }
                    logger.info(
                        "BLAST IMPULSE DEBUG player=%s %r",
                        target.name,
                        target.last_explosion_impulse_debug,
                    )
                if not deferred_prediction:
                    vx, vy, vz = target.velocity
                    target.velocity = (
                        vx + impulse_preview[0],
                        vy + impulse_preview[1],
                        vz + impulse_preview[2],
                    )

            # The client applies explosion velocity before its ordinary damage
            # policy. Friendly-fire-off therefore suppresses HP loss but does
            # not suppress the physical push.
            if (
                thrower is not None
                and target is not thrower
                and target.team == thrower.team
                and not getattr(self.config, "friendly_fire", False)
            ):
                continue
            amount = retail.explosion_player_damage(
                origin, position, blast_radius, damage,
                crouched=crouched,
                los_fraction=los_fraction,
                is_self=thrower is not None and target is thrower,
                target_team=getattr(target, "team", None),
                classic=classic,
            ) * retail.victim_damage_multiplier(target)
            # Player.damage applies mode/rule multipliers before the single
            # HP rounding step. Preserve blast fractions through that policy.
            if amount > 0.0:
                target.damage(amount, source=thrower, kill_type=int(kill_type))

        # Damageable placed entities share the same LOS/radius/falloff as
        # players. Iterate a snapshot because a one-hit C4/medpack may remove
        # itself from the registry during on_damage.
        entity_ctx = self._build_entity_ctx()
        for entity in list(self.entity_registry.all()):
            behavior = getattr(entity, "behavior", None)
            if (
                not entity.alive
                or behavior is None
                or not getattr(behavior, "takes_damage", False)
            ):
                continue
            raw_center = behavior.get_hit_center(entity)
            center = (float(raw_center[0]), float(raw_center[1]),
                      float(raw_center[2]))
            # Stock non-player damageable: D * (R^2 - d^2) / R^2 at its own
            # position, one sight ray (10%..110%), all-or-nothing.
            entity_damage = retail.explosion_entity_damage(
                origin, center, blast_radius, damage
            )
            if entity_damage <= 0.0:
                continue
            if segment_blocked is not None and retail.los_ray_blocked(
                segment_blocked, origin, center
            ):
                continue
            self.entity_registry.damage_entity(
                entity.entity_id, entity_damage, thrower, entity_ctx
            )

    def _blocked_los(self, x0, y0, z0, x1, y1, z1) -> bool:
        dx, dy, dz = x1 - x0, y1 - y0, z1 - z0
        dist = (dx * dx + dy * dy + dz * dz) ** 0.5
        if dist < 1e-6:
            return False
        hit = self.world_manager.raycast(x0, y0, z0, dx / dist, dy / dist, dz / dist, dist - 0.5)
        return hit is not None

    def _build_entity_ctx(self):
        """Build the per-tick EntityContext handed to entity behaviors. Players
        are pre-filtered to alive + spawned so behaviors never re-check."""
        from server.entities.registry import EntityContext
        players = [p for p in self.players.values() if p.alive and p.spawned]
        return EntityContext(
            dt=self.tick_interval,
            now=time.time(),
            players=players,
            world=self.world_manager,
            server=self,
            create=self.broadcast_create_entity,
            destroy=self.broadcast_destroy_entity,
            move=self.broadcast_change_entity_position,
        )

    def _broadcast_create_player(self, player, spawn) -> None:
        """Re-announce a (re)spawned player to all clients as alive."""
        from shared.packet import CreatePlayer
        from server.connection import internal_team_to_wire

        packet = CreatePlayer()
        packet.player_id = player.id
        packet.demo_player = 0
        packet.class_id = player.class_id
        packet.team = internal_team_to_wire(player.team)
        packet.dead = 0
        packet.local_language = getattr(player, 'local_language', 0)
        packet.x, packet.y, packet.z = spawn[0], spawn[1], spawn[2]
        # Real orientation unit vector — never a degenerate (0,0,255.5), which
        # NaNs the client's non-local-player look-at basis and crashes the
        # renderer natively. See connection.py spawn path.
        packet.ori_x = player.o_x
        packet.ori_y = player.o_y
        packet.ori_z = player.o_z
        packet.name = player.name
        packet.loadout = list(getattr(player, 'loadout', []) or [])
        packet.prefabs = list(getattr(player, 'prefabs', []) or [])
        self.broadcast(bytes(packet.generate()))
        from server.roster import remember_player_life
        for connection in self.connections.values():
            if getattr(connection, "in_game", False):
                remember_player_life(connection, player)
        # A peer that joined while this player was dead may be seeing their
        # first CreatePlayer now. Restore the existing score after that row.
        from server.scoreboard import player_score_packet
        self.broadcast(player_score_packet(player))
        from shared.packet import SetColor
        color = SetColor()
        color.player_id = player.id
        color.value = int(player.block_color) & 0xFFFFFF
        self.broadcast(bytes(color.generate()))

    def _repair_dead_join_respawn(self, connection) -> None:
        """Deliver a dead joiner's own first life if it began while gated.

        A joiner the mode kept dead (Connection._send_join_death) respawns
        through the gameplay-gated broadcast. If that respawn happened
        before its first ClientData, the client never saw its own new
        CreatePlayer, and catch_up_roster deliberately skips the local id.
        """
        token = getattr(connection, "join_death_token", None)
        player = getattr(connection, "player", None)
        if token is None or player is None:
            return
        connection.join_death_token = None
        from server.roster import build_create_player, player_life_token

        if not (player.alive and player.spawned):
            return
        if player_life_token(player) == token:
            return
        connection.send(bytes(build_create_player(player).generate()), reliable=True)
        send_hp = getattr(connection, "_send_spawn_hp", None)
        if callable(send_hp):
            send_hp()

    def _send_join_audio(self, connection) -> None:
        """Start map ambience and the right music track on one joiner.

        A mid-round joiner must get both directly (the round-start broadcast
        fired before it arrived). The mode picks the track (the game_ending
        track in the final minute or on the end screen); otherwise a random
        gameplay bed. ``play_music_to`` sends StopMusic+PlayMusic, which also
        clears the client's leftover menu music.
        """
        if getattr(connection, "join_audio_sent", False):
            return
        try:
            from server.audio import send_map_ambient, play_music_to, \
                gameplay_bed_track
            player = getattr(connection, "player", None)
            if player is not None:
                send_map_ambient(self, player)
            track = None
            pick = getattr(getattr(self, "mode", None), "join_music_track", None)
            if callable(pick):
                track = pick()
            # [audio] mode_start_music = false: no bed, retail silence.
            track = track or gameplay_bed_track(self)
            if track is not None:
                play_music_to(connection, track)
            connection.join_audio_sent = True
        except Exception:
            logger.debug("reveal ambient/music send failed", exc_info=True)

    def reveal_world_to(self, connection) -> bool:
        """Send a now-in-game client the map entities (crates). Called from the
        connection's FIRST ClientData, never during the join handshake — a
        flood of entity creates while the client is still building the world /
        mid-GameScene-transition crashes the compiled client natively.

        The player roster is reconciled by concrete life token here. Two
        clients can both snapshot an empty roster before either creates its
        player, then mutually miss their gameplay-gated CreatePlayer packets.
        Token catch-up sends only lives absent from that client's handshake.

        The caller sets connection.in_game only after this complete reveal, so
        ongoing gameplay broadcasts cannot interleave with catch-up.
        """
        # World ambience + music for this now-settled client, BEFORE any
        # catch-up below. The terrain/roster replay is a burst of hit/build
        # effects that exhausts the stock client's 128 OpenAL sources; a
        # stream started after it fails (live 2026-09-26: ~1550 replayed
        # Damage -> 191 "Error starting source", amb_city and the music bed
        # never loaded). Sent once per scene epoch: a large air-override
        # history returns False here and resumes on the next ClientData.
        self._send_join_audio(connection)

        # MapSync is still the efficient bulk path, but the retail VXL worker
        # has a native collision/mesh cache that can retain stale solids after
        # a heavily drilled column merge. Clear every pre-snapshot destroyed
        # cell through the proven exact packet path before gameplay broadcasts
        # are admitted. Large histories drain over consecutive ClientData
        # frames so one reconnect cannot monopolize the authoritative tick.
        if not self.replay_map_air_overrides(connection):
            return False

        from server.roster import catch_up_roster
        catch_up_roster(self, connection)
        self._repair_dead_join_respawn(connection)

        # BlockLine/BlockBuild packets carry no RGB. Refresh every sender's
        # current palette before replaying terrain so late joiners render the
        # authoritative VXL colours.
        from shared.packet import SetColor
        joining_player = getattr(connection, "player", None)
        for roster_player in self.players.values():
            if roster_player is joining_player:
                # The joiner's own palette may be a choice still in flight to
                # the server; echoing the stale value would overwrite it.
                continue
            color = SetColor()
            color.player_id = roster_player.id
            color.value = int(roster_player.block_color) & 0xFFFFFF
            connection.send(bytes(color.generate()), reliable=True)

        # CreatePlayer carries a loadout but no current equipped tool/action
        # state. Do not leave late join initialization to the next unreliable
        # 30 Hz packet: one reliable remote-only snapshot makes every newly
        # revealed Character immediately match the authoritative life. The
        # local row is deliberately excluded, so this cannot reconcile or
        # move the joining owner on its first input frame.
        local_player = getattr(connection, "player", None)
        local_player_id = (
            int(local_player.id) if local_player is not None else None
        )
        roster_snapshot = self.build_world_update_data(
            exclude_player_id=local_player_id,
            loop_count_override=int(self.loop_count),
        )
        connection.send(roster_snapshot, reliable=True)

        # First close the terrain gap between the MapSync snapshot and this
        # first ClientData. This is still synchronous on the server event loop,
        # so no live mutation can interleave between replay and in_game=True.
        self.replay_map_mutations(connection)
        # Per-cell block health (prefab cells start at 9) via
        # BlockManagerState(38): MapSync/33 alone would leave the joiner at 5/3.
        prefab_reveal = getattr(getattr(self, "prefab_actions", None), "reveal_to", None)
        if callable(prefab_reveal):
            prefab_reveal(connection)

        from server.scoreboard import reveal_to as reveal_scores
        reveal_scores(self, connection)

        # CreatePlayer intentionally has no safe no-pickup sentinel. Genuine
        # carriers must be announced even when generic entity replication is
        # disabled, using the dedicated packet that initializes the carried
        # tool and burden state.
        from shared.packet import PickPickup
        for carrier in self.players.values():
            pickup_id = getattr(carrier, "pickup_id", None)
            if pickup_id is None:
                continue
            packet = PickPickup()
            packet.player_id = int(carrier.id)
            packet.pickup_id = int(pickup_id)
            packet.burdensome = int(bool(carrier.pickup_burdensome))
            connection.send(bytes(packet.generate()), reliable=True)

        if getattr(self.config, "entities_wire_ready", False):
            from server.entities.registry import send_create_entity_to
            for ent in self.entity_registry.static_entities():
                try:
                    send_create_entity_to(connection, ent)
                except Exception:
                    logger.debug("reveal entity send failed", exc_info=True)

        # Mode-owned UI/objective state is not part of the static entity
        # registry. CTF uses this post-GameScene hook for native base zones and
        # the current carrier marker; it must run even when generic entity
        # replication is disabled.
        reveal_mode_state = getattr(self.mode, "reveal_to", None)
        if reveal_mode_state is not None:
            try:
                reveal_mode_state(connection)
            except Exception:
                logger.debug("mode reveal send failed", exc_info=True)
        # Late joiners: live minimap billboards and runtime team rules (81/82).
        try:
            from server.hud_packets import reveal_hud_state

            reveal_hud_state(self, connection)
        except Exception:
            logger.debug("hud reveal failed", exc_info=True)

        # Radar needs no extra join replay: live stations are ordinary
        # deployable entities in the CreateEntity replay above, and the
        # client's Minimap runs RadarStationEntity.can_detect_player itself.

        # Gameplay broadcasts are gated until this first ClientData, so a
        # player who loaded during the final map ballot missed its original
        # GenericVoteMessage. Replay only after the GameScene, terrain, roster,
        # and entities are complete; packet 47 is unsafe in LoadingMenu.
        vote_manager = getattr(self, "vote_manager", None)
        reveal_vote = getattr(vote_manager, "reveal_to", None)
        if callable(reveal_vote):
            reveal_vote(connection)

        return True

    # Radar stations send no TeamMapVisibility(83). The stock client's
    # Minimap asks each of the viewer team's RadarStationEntity objects
    # ``can_detect_player`` (250 blocks, C.RADAR_STATION_RANGE) for every
    # enemy, so the packet-21 entity (team + position + fuse) is all it
    # needs. Packet 83 sets ``teams[team_id].can_see_other_team``, a
    # whole-team reveal with no range, which retail radar did not do
    # (verified live 2026-09-26, docs/RETAIL_VALUES.md "Radar station").
    def _radar_station_added(self, team: int) -> None:
        team = int(team)
        count = int(self._radar_station_counts.get(team, 0)) + 1
        self._radar_station_counts[team] = count

    def _radar_station_removed(self, team: int) -> None:
        team = int(team)
        count = max(0, int(self._radar_station_counts.get(team, 0)) - 1)
        self._radar_station_counts[team] = count

    def broadcast_create_entity(self, map_entity) -> None:
        """Announce a placed entity (crate/intel/...) to all clients."""
        if not getattr(map_entity, "wire_visible", True):
            # Legacy objective markers can exist for authoritative mode logic
            # without being legal packet-21 entities in the retail client.
            return
        from server.entities.registry import send_create_entity_to
        for connection in tuple(self.connections.values()):
            if not bool(getattr(connection, "in_game", False)):
                continue
            send_create_entity_to(connection, map_entity)

    def broadcast_known_entity_packet(
        self,
        data: bytes,
        entity_id: int,
        *,
        reliable: bool = True,
    ) -> None:
        """Send an entity-referencing packet only to peers that saw CreateEntity.

        The native Damage handler dereferences ``causer_id`` without a safe
        missing-entity fallback. A peer crossing MapSync may be in-game without
        having observed a short-lived charge, so ordinary global broadcast is
        not safe for this packet family.
        """

        entity_id = int(entity_id)
        for connection in tuple(self.connections.values()):
            if not bool(getattr(connection, "in_game", False)):
                continue
            known = getattr(connection, "known_entity_ids", ())
            if entity_id not in known:
                continue
            connection.send(data, reliable=reliable)

    def broadcast_known_player_packet(
        self,
        data: bytes,
        player_id: int,
        *,
        reliable: bool = True,
        exclude=None,
    ) -> int:
        """Send a packet naming ``player_id`` only to peers that know that id.

        The retail GameScene indexes its player table directly for SetScore,
        ChatMessage, ExplodeCorpse, SetColor and PlayerLeft; an id it never
        received a CreatePlayer for (a dead joiner, a player created while the
        peer was loading, a departed id) raises inside the packet handler.
        ``known_player_lives`` is exactly the set of ids this connection was
        told about. Connections without the ledger (legacy embedders/test
        doubles) keep the historical broadcast contract. Returns the number
        of connections the packet was queued to.
        """

        player_id = int(player_id)
        sent = 0
        for connection in tuple(self.connections.values()):
            if not bool(getattr(connection, "in_game", False)):
                continue
            if exclude is not None and getattr(connection, "player", None) is exclude:
                continue
            known = getattr(connection, "known_player_lives", None)
            if known is not None and player_id not in known:
                continue
            connection.send(data, reliable=reliable)
            sent += 1
        return sent

    def _forget_departed_player_id(self, player_id: int) -> None:
        """Drop a departed id from every in-game peer's roster ledgers.

        Runs right after PlayerLeft went to the in-game peers that knew the
        id. Loading peers keep their entry: ``roster.catch_up_roster`` sends
        their PlayerLeft for the stale id on first ClientData. Forgetting the
        id also makes later packets naming the departed id (a leaver's queued
        SetScore, ExplodeCorpse, chat) reach nobody.
        """
        player_id = int(player_id)
        for connection in tuple(self.connections.values()):
            if not bool(getattr(connection, "in_game", False)):
                continue
            for ledger_name in (
                "known_player_lives",
                "known_player_deaths",
                "known_corpse_cleanups",
            ):
                ledger = getattr(connection, ledger_name, None)
                if isinstance(ledger, dict):
                    ledger.pop(player_id, None)

    def broadcast_change_entity_position(self, map_entity) -> None:
        """Move an existing static entity without duplicate create/destroy.

        Packet 16 action 1 is ``SET_POSITION`` in the retail client.  It is
        reliable because a missed pickup fall would leave different collision
        targets and bot/resource state on different peers.
        """
        from shared.packet import ChangeEntity

        packet = ChangeEntity()
        packet.entity_id = int(map_entity.entity_id)
        packet.action = 1
        packet.pos_x = float(map_entity.x)
        packet.pos_y = float(map_entity.y)
        packet.pos_z = float(map_entity.z)
        self.broadcast(bytes(packet.generate()), reliable=True)

    def spawn_projectile_entity(self, projectile, owner, pos, vel) -> None:
        """Create the visible client entity for a server-owned projectile."""
        if projectile is None or not projectile.spec.entity_type:
            return
        from server.connection import internal_team_to_wire
        team = getattr(owner, "team", TEAM1)
        player_id = getattr(owner, "id", 0)
        ent = self.entity_registry.place(
            int(projectile.spec.entity_type),
            float(pos[0]), float(pos[1]), float(pos[2]),
            state=internal_team_to_wire(team), kind="projectile",
            player_id=player_id,
            vel=(float(vel[0]), float(vel[1]), float(vel[2])),
            radius=0.02,
        )
        projectile.entity_id = ent.entity_id
        self.broadcast_create_entity(ent)

    def broadcast_turret_properties(self, turret) -> None:
        """Update the stock client's turret lock target and ammo display."""
        from shared.packet import ChangeEntity
        target = ChangeEntity()
        target.entity_id = int(turret.entity_id)
        target.action = 5  # SET_TARGET
        target.target_id = -1 if turret.target_id is None else int(turret.target_id)
        self.broadcast(bytes(target.generate()), reliable=True)

        ammo = ChangeEntity()
        ammo.entity_id = int(turret.entity_id)
        ammo.action = 7  # SET_AMMO
        ammo.ammo = float(turret.ammo)
        self.broadcast(bytes(ammo.generate()), reliable=True)

    def broadcast_destroy_entity(self, entity_id: int) -> None:
        """Destroy an entity only in GameScenes that received its creation.

        Gameplay gating alone is insufficient at the join boundary: moving
        projectiles are intentionally absent from the static entity snapshot.
        If one was created during MapSync and expired immediately after the
        peer entered GameScene, a global DestroyEntity referred to an ID that
        client had never allocated. Per-connection create knowledge preserves
        the native create/destroy symmetry without replaying stale projectiles.
        """
        from shared.packet import DestroyEntity

        entity_id = int(entity_id)
        pkt = DestroyEntity()
        pkt.entity_id = entity_id
        data = bytes(pkt.generate())
        for connection in tuple(self.connections.values()):
            if not bool(getattr(connection, "in_game", False)):
                continue
            known = getattr(connection, "known_entity_ids", None)
            # Every real Connection owns this set.  A missing attribute means
            # a legacy embedding/test connection that predates per-scene
            # knowledge, so preserve the historical broadcast contract for
            # that compatibility facade.  An explicit set remains strict and
            # protects retail GameScene from destroy-without-create crashes.
            if known is not None and entity_id not in known:
                continue
            connection.send(data, reliable=True)
            if known is not None:
                known.discard(entity_id)

    def broadcast_state_data(self) -> None:
        """Re-send StateData(45) to every in-game client.

        DANGER: do NOT use this for routine score updates. The compiled client
        treats a mid-game StateData as a scene (RE)INITIALISATION — it reloads
        the prefabs ('supertower'), tears down and recreates the UGC palette
        ("delete ugc palette" in the client log) and crashes natively a few
        frames later (measured 2026-06-14). Use broadcast_set_score() for
        scores. Reserve this for a deliberate, rare full-state refresh.
        """
        from server.builders import build_state_data

        for connection in list(self.connections.values()):
            if not connection.in_game:
                continue  # mid-transition; caught up on first ClientData
            try:
                player = getattr(connection, "player", None)
                player_id = int(player.id) if player is not None else -1
                state = build_state_data(self, player_id=player_id)
                data = bytes(state.generate())
                connection.send(data, prefix=0x31)
            except Exception:
                logger.debug("broadcast_state_data: send failed", exc_info=True)

    def broadcast_set_score(self, team, *, reason: int | None = None) -> None:
        """Update one team's HUD score on every in-game client via the
        lightweight SetScore(85) packet — the correct mid-game score update.
        Unlike StateData it carries no scene/prefab/UGC data, so the client
        just sets team.score and redraws the HUD (no re-init, no crash)."""
        from shared.packet import SetScore
        from server.connection import internal_team_to_wire
        from shared.constants import SCORE, SCORE_REASON

        pkt = SetScore()
        pkt.type = int(SCORE.TEAM)
        pkt.reason = (
            int(SCORE_REASON.KILL_SCORE_REASON)
            if reason is None
            else int(reason)
        )
        pkt.specifier = internal_team_to_wire(team.id)
        pkt.value = int(team.score)
        self.broadcast(bytes(pkt.generate()))

    async def start(self):
        """Initialize and start the server."""
        import enet

        if self._stopping or self._stopped:
            raise RuntimeError("a stopped BattleSpadesServer cannot be restarted")

        # Fail fast before binding sockets: an unknown mode used to start a
        # server with mode=None (no rules, scoring or round end). Launchers
        # register tut/ugc before start(), so those resolve here too.
        from modes import get_mode_class, registered_mode_codes
        if get_mode_class(self.config.game_mode) is None:
            raise ValueError(
                f"game.default_mode {self.config.game_mode!r} is not a "
                f"registered game mode; expected one of: "
                f"{', '.join(registered_mode_codes())}"
            )

        # Windows default timer granularity is ~15.6ms, which makes
        # asyncio.sleep bursty and the 60Hz tick/broadcast jittery (the
        # client sees irregular WorldUpdate spacing as movement jank).
        # Request 1ms resolution for the lifetime of the process.
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
                logger.info("Windows timer resolution set to 1ms")
            except Exception as exc:
                logger.warning(f"timeBeginPeriod failed: {exc}")

        logger.info(f"Starting BattleSpades server on port {self.config.port}")
        
        # pyenet reports bind collisions as MemoryError. Probe and translate
        # host creation so an operator sees the real endpoint failure.
        self.host = _create_enet_host(
            enet,
            port=self.config.port,
            max_connections=self.config.max_connections,
        )
        
        # Enable compression like reference
        self.host.compress_with_range_coder()
        
        # Set intercept for A2S queries (reference pattern)
        self.host.intercept = self._intercept
        logger.info("A2S/LAN intercept registered")
        
        # Load map
        self.world_manager.load_map(self.config.map_name)
        # Before the mode starts: its first round already counts.
        self.achievements.start()
        
        # Initialize game mode (validated at the top of start()).
        mode_class = get_mode_class(self.config.game_mode)
        self.mode = mode_class(self)
        await self.mode.on_mode_start()

        # Product/map/mode identity must be established before anonymous Steam
        # logon. Missing optional runtime files do not prevent local hosting.
        await self.steam_master.start()
        await self.revival_master.start()
        await self.steam_p2p.start()
        # After the master: the relay hello carries this server's AoSPlay id.
        await self.steam_host.start()
        await self.a2s_handler.start()

        # Auto-discover + load plugins from the plugins/ package.
        await self._load_plugins()

        # Start bots only after the active map and mode exist. An explicit
        # [bots] table supersedes legacy game.bot_count; the legacy value keeps
        # fixed-count behavior for existing deployments.
        bot_config = getattr(self.config, "bots", None)
        has_explicit_bots = bool(
            bot_config is not None
            and getattr(bot_config, "configured", False)
        )
        explicit_bots = bool(
            has_explicit_bots and getattr(bot_config, "enabled", False)
        )
        legacy_bot_count = int(getattr(self.config, "bot_count", 0) or 0)
        if explicit_bots or (not has_explicit_bots and legacy_bot_count > 0):
            from server.bot_ai import BotDirector

            self.bots = BotDirector(self)
            initial_count = None if explicit_bots else legacy_bot_count
            if not explicit_bots and bot_config is not None:
                bot_config.population_mode = "fixed"
                bot_config.max_bots = legacy_bot_count
            await self.bots.start(initial_count=initial_count)
            logger.info("Bot runtime started with %d bot(s)", len(self.bots.bots))

        self.running = True
        logger.info(f"Server started: {self.config.server_name}")
        
        # Run main loops. WorldUpdate broadcasting happens inside
        # _game_loop right after each simulated tick (state and loop_count
        # must be sampled atomically — see _game_loop).
        await asyncio.gather(
            self._network_loop(),
            self._game_loop(),
        )
    
    async def stop(self) -> None:
        """Stop gameplay and retire all ENet borrowers before the host."""

        async with self._stop_lock:
            if self._stopped:
                return

            logger.info("Stopping server...")
            # This is the native ownership gate.  It is intentionally raised
            # before the first await so cancellation callbacks and delayed
            # mode/handshake work cannot enqueue another packet while cleanup
            # is in progress.
            self._stopping = True
            self.running = False
            self.a2s_handler.stop()

            try:
                await self._deactivate_mode_for_shutdown()
                await self._cancel_connection_tasks()

                if self.bots is not None:
                    try:
                        await self.bots.close()
                    except Exception:
                        logger.exception("Bot runtime shutdown failed")
                    finally:
                        self.bots = None

                for name, service in (
                    ("Steam relay", getattr(self, "steam_p2p", None)),
                    ("Steam relay host", getattr(self, "steam_host", None)),
                    ("Steam master", self.steam_master),
                    ("Revival master", self.revival_master),
                ):
                    if service is None:
                        continue
                    try:
                        await service.close()
                    except Exception:
                        logger.exception("%s shutdown failed", name)

                if self.debug_parity is not None:
                    try:
                        self.debug_parity.close()
                    except Exception:
                        logger.exception("Debug parity shutdown failed")

                try:
                    self.prefab_actions.close()
                except Exception:
                    logger.exception("Prefab runtime shutdown failed")

                try:
                    self.achievements.close()
                except Exception:
                    logger.exception("Achievement store shutdown failed")
            finally:
                # This block is synchronous on purpose: even cancellation of
                # stop() itself must not leave a live Python Connection beside
                # a destroyed native Host.
                self._retire_network_for_shutdown()
                self._stopped = True

            logger.info("Server stopped")

    async def _deactivate_mode_for_shutdown(self) -> None:
        """Cancel delayed match work and retire the mode without victory UI."""

        mode = self.mode
        if mode is None:
            return

        cancel = getattr(mode, "cancel_end_sequence", None)
        if callable(cancel):
            try:
                await cancel()
            except Exception:
                logger.exception("Mode end-sequence cancellation failed")

        deactivate = getattr(mode, "deactivate", None)
        if callable(deactivate):
            try:
                await deactivate()
            except Exception:
                logger.exception("Mode deactivation failed")

    async def _cancel_connection_tasks(self) -> None:
        """Cancel pre-join receive/handshake work owned by this server."""

        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in self._connection_tasks
            if task is not current and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connection_tasks.clear()

    def _retire_network_for_shutdown(self) -> None:
        """Detach connections, then flush and release their owning ENet host."""

        connection_items = tuple(self.connections.items())
        # Clear the authoritative registry first.  Broadcasts are already
        # gated by ``_stopping``, and no late task can rediscover a Connection
        # after its peer's memory is released.
        self.connections.clear()
        self._pending_ingame_packets.clear()
        self._mode_events.clear()
        self.reserved_player_ids.clear()

        for peer, connection in connection_items:
            player = getattr(connection, "player", None)
            if (
                player is not None
                and getattr(player, "connection", None) is connection
            ):
                player.connection = None
            retire = getattr(connection, "retire_for_server_shutdown", None)
            if callable(retire):
                retire()
            try:
                # The Host still owns peer memory at this point.
                peer.disconnect()
            except Exception:
                logger.debug("ENet peer disconnect failed during shutdown", exc_info=True)

        self.players.clear()
        for team in self.teams.values():
            team.players.clear()
            team.intel_holder = None

        # Drop the local Peer/Connection tuple before the Host.  External
        # references remain harmless because Connection.send/disconnect are
        # guarded by ``_stopping``.
        if connection_items:
            del peer, connection, player, retire
        connection_items = ()
        host = self.host
        if host is not None:
            try:
                host.flush()
            except Exception:
                logger.debug("ENet host flush failed during shutdown", exc_info=True)
            finally:
                self.host = None
        host = None
    
    def _intercept(self, address, data: bytes):
        """Intercept raw UDP packets for A2S/LAN queries."""
        # Handle A2S queries here
        return self.a2s_handler.intercept(address, data)
    
    def _net_update(self):
        """Process ENet events - synchronous, called from network loop."""
        import enet
        
        for _ in range(self.config.network_event_budget):
            if self.host is None:
                return
            
            try:
                event = self.host.service(0)
                event_type = event.type
                if not event or event_type == enet.EVENT_TYPE_NONE:
                    return
                
                peer = event.peer
                
                if event_type == enet.EVENT_TYPE_CONNECT:
                    logger.info(f"ENET CONNECT from {peer.address} data={event.data}")
                    self._on_connect_sync(peer, event.data)
                    
                elif event_type == enet.EVENT_TYPE_DISCONNECT:
                    logger.info(f"ENET DISCONNECT from {peer.address}")
                    self._on_disconnect_sync(peer)
                    
                elif event_type == enet.EVENT_TYPE_RECEIVE:
                    connection = self.connections.get(peer)
                    data = bytes(event.packet.data)
                    if connection is not None and connection.player is not None:
                        # In-game traffic: queue for the tick-start drain
                        # (deterministic ordering relative to simulation).
                        self._queue_ingame_packet(connection, data)
                    else:
                        # Pre-join flows (handshake, map transfer) can be
                        # slow — keep them off the simulation path.
                        task = asyncio.create_task(
                            self._on_receive_data(peer, data),
                            name="BattleSpades-prejoin-receive",
                        )
                        self._connection_tasks.add(task)
                        task.add_done_callback(self._connection_tasks.discard)
                    
            except Exception as e:
                logger.error(f"Error in net_update: {e}", exc_info=True)
    
    def _per_connection_packet_cap(self) -> int:
        """Most in-game packets one connection may hold in the shared queue.

        The global queue is drained once per tick. Without a per-peer share a
        single flooding client filled all ``max_pending_packets`` slots and
        every other player's ClientData was dropped at the global cap.
        """
        total = max(1, int(getattr(self.config, "max_pending_packets", 4096)))
        configured = getattr(self.config, "max_pending_packets_per_connection", None)
        if configured is not None:
            return max(1, min(total, int(configured)))
        # 256 rows is >4 s of 60 Hz input for one peer: far above any honest
        # backlog, while 16 flooding peers still cannot starve the rest.
        return max(1, min(total, 256))

    def _pending_packet_counts(self) -> dict:
        """Per-connection share of ``_pending_ingame_packets`` (self-healing).

        Other owners clear the shared deque wholesale (timeline resets,
        shutdown); an empty deque therefore always resets the shares.
        """
        counts = getattr(self, "_pending_ingame_counts", None)
        if counts is None:
            counts = {}
            self._pending_ingame_counts = counts
        if not self._pending_ingame_packets and counts:
            counts.clear()
        return counts

    def _queue_ingame_packet(self, connection, data: bytes) -> bool:
        """Queue one in-game packet, dropping a flooding peer's own excess."""
        counts = self._pending_packet_counts()
        key = id(connection)  # test doubles/embedders may be unhashable
        queued = counts.get(key, 0)
        if (
            queued >= self._per_connection_packet_cap()
            or len(self._pending_ingame_packets) >= self.config.max_pending_packets
        ):
            self._dropped_ingame_packets += 1
            self.metrics.dropped_ingame_packets += 1
            return False
        self._pending_ingame_packets.append((connection, data))
        counts[key] = queued + 1
        return True

    async def _network_loop(self):
        """Handle ENet events."""
        while self.running:
            if self.host is None:
                break
            
            self._net_update()
            # Service ENet aggressively (1ms) so client inputs are applied
            # on the next simulation tick with minimal jitter; with the old
            # 60Hz polling an input could wait a full extra tick.
            await asyncio.sleep(0.001)
    
    async def _game_loop(self):
        """Compatibility entry point for the fixed-step runtime service."""
        runtime = getattr(self, "simulation_runtime", None)
        if runtime is None:
            runtime = SimulationRuntime(self)
            self.simulation_runtime = runtime
        await runtime.run()
    
    def _broadcast_world_updates(self) -> None:
        """Compatibility delegate to grouped snapshot replication."""
        replication = getattr(self, "replication", None)
        if replication is None:
            replication = ReplicationService(self)
            self.replication = replication
        replication.broadcast_world_updates()

    def _log_selfrow(self, player, stamp: int) -> None:
        """Queue one self-row diagnostic sample without gameplay-thread I/O."""
        manager = getattr(self, "debug_parity", None)
        writer = getattr(manager, "write_selfrow_sample", None)
        if callable(writer):
            writer(player, stamp)

    def build_world_update_packet(
        self,
        exclude_player_id: Optional[int] = None,
        loop_count_override: Optional[int] = None,
        local_player_id: Optional[int] = None,
    ) -> WorldUpdate:
        """Compatibility delegate for tests and packet tooling."""
        replication = getattr(self, "replication", None)
        if replication is None:
            replication = ReplicationService(self)
            self.replication = replication
        return replication.build_world_update_packet(
            exclude_player_id,
            loop_count_override,
            local_player_id,
        )

    @staticmethod
    def _self_world_update_is_safe(player) -> bool:
        """Compatibility wrapper for the native block-tool exception."""
        return ReplicationService.self_row_is_safe(player)

    def build_world_update_data(
        self,
        exclude_player_id: Optional[int] = None,
        loop_count_override: Optional[int] = None,
        local_player_id: Optional[int] = None,
    ) -> bytes:
        """Compatibility delegate for grouped snapshot serialization."""
        replication = getattr(self, "replication", None)
        if replication is None:
            replication = ReplicationService(self)
            self.replication = replication
        return replication.build_world_update_data(
            exclude_player_id,
            loop_count_override,
            local_player_id,
        )
    
    def _on_connect_sync(self, peer, data: int = 0):
        """Handle new connection (sync version for net_update)."""
        logger.info(f"New connection from {peer.address} (proto_ver={data})")

        # Retail GameClient connects with shared.steam.game_version() (168)
        # as the ENet connect data; the native client sends 168 too.  Refuse
        # anything else with the retail version reasons before allocating.
        if bool(getattr(self.config, "require_protocol_version", True)):
            reason = protocol_version_refusal(data)
            if reason is not None:
                logger.info(
                    "Rejected client %s with protocol %s (reason %d)",
                    peer.address, data, reason,
                )
                try:
                    peer.disconnect(reason)
                except Exception:
                    pass
                return

        # Reject banned IPs before we allocate any state for them.
        from server.bans import address_host
        ban = self.ban_manager.is_banned(address_host(peer, self))
        if ban is not None:
            logger.info("Rejected banned client %s (%s)", peer.address, ban.get("reason"))
            try:
                # ERROR_TEMP_BANNED (19) for a ban with an expiry, else
                # ERROR_BANNED (1).
                peer.disconnect(19 if ban.get("until") else 1)
            except Exception:
                pass
            return
        # A vote-kick lasts "until the end of the current match" (client text).
        vote_manager = getattr(self, "vote_manager", None)
        kick_reason = getattr(vote_manager, "match_kick_reason", lambda _host: None)(
            address_host(peer, self)
        )
        if kick_reason is not None:
            logger.info("Rejected vote-kicked client %s until the match ends", peer.address)
            try:
                peer.disconnect(int(kick_reason))
            except Exception:
                pass
            return

        # Check if connection already exists
        connection = self.connections.get(peer)
        if connection is None:
            connection = Connection(peer, self)
            self.connections[peer] = connection
            self._configure_peer_throttle(peer)
        
        # Call connection's on_connect
        connection.on_connect(data)
    
    def _configure_peer_throttle(self, peer) -> None:
        """Keep unreliable WorldUpdates flowing on jittery links.

        ENet's per-peer packet throttle drops a share of unreliable sends after
        round trips that look worse than the recent variance allows, for up to
        five seconds at a time. ``unreliable_throttle_deceleration = 0`` (the
        default) stops it ever lowering that share; see ServerConfig.
        """
        deceleration = int(getattr(
            self.config, "unreliable_throttle_deceleration", 0
        ))
        try:
            peer.packetThrottleDeceleration = deceleration
        except (AttributeError, TypeError, ValueError):
            # Older/other ENet bindings: keep the library default.
            logger.debug("peer throttle not configurable for %s", peer.address)

    def _on_disconnect_sync(self, peer):
        """Handle disconnection (sync version for net_update)."""
        connection = self.connections.pop(peer, None)
        if not connection:
            relay = getattr(self, "steam_p2p", None)
            if relay is not None:
                relay.forget_peer(peer)
            return

        # RECEIVE and DISCONNECT can be serviced in the same ENet pump.  A
        # packet queued before the disconnect must not run on the next tick:
        # player ids are deliberately reused, so a stale deployable packet can
        # otherwise create an entity owned by a completely different player.
        if self._pending_ingame_packets:
            self._pending_ingame_packets = deque(
                (queued_connection, data)
                for queued_connection, data in self._pending_ingame_packets
                if queued_connection is not connection
            )
        counts = getattr(self, "_pending_ingame_counts", None)
        if counts is not None:
            counts.pop(id(connection), None)
        # A connection may disconnect after taking a MapSync watermark but
        # before first ClientData. Once it is gone, it must no longer pin the
        # terrain catch-up journal at an old sequence indefinitely.
        self._prune_map_mutations()

        # Release an id promised to a client that never finished joining.
        reserved = getattr(connection, "reserved_player_id", None)
        if reserved is not None:
            self.reserved_player_ids.discard(reserved)
        
        if connection.player:
            player = connection.player
            logger.info(f"Player {player.name} disconnected")

            revival_master = getattr(self, "revival_master", None)
            if revival_master is not None:
                revival_master.accumulate_departing_player(player)

            # Mode state (VIP ownership, CTF intel, bomb, diamond) must be
            # released while the departing id is still a valid roster entry:
            # the drop packets it emits name this player, so they must reach
            # clients BEFORE PlayerLeft. Queuing the hook to the next tick
            # (the old behaviour) sent them after PlayerLeft, naming an id the
            # clients had already destroyed. Same order as the transition
            # path's _detach_transition_player.
            self._run_player_leave_hooks(player)

            # Numeric player ids are reused from the lowest free slot. Retire
            # every owner-sensitive producer/cache before exposing this id to
            # another connection; RoundLifecycle preserves ordinary world
            # construction while removing deployables and stale credit.
            self.round_lifecycle.forget_player(player)
            achievements.player_left(self, player)
            
            # Remove from team
            if player.team in self.teams:
                self.teams[player.team].remove_player(player)
            
            # Remove from players
            self.players.pop(player.id, None)
            
            # Broadcast disconnect, but only to GameScenes that were told
            # about this id. A dead joiner (or a player whose only life began
            # while a peer was still loading) was never created on those
            # peers; PlayerLeft for an unknown id fails in the retail roster
            # handler. Forget the id on each recipient so a later reuse of
            # the number starts from a clean ledger.
            self._announce_player_left(connection, player)
            left_packet = PlayerLeft()
            left_packet.player_id = player.id
            self.broadcast(
                bytes(left_packet.generate()), known_player_id=int(player.id)
            )
            self._forget_departed_player_id(int(player.id))
        
        connection.on_disconnect()
        relay = getattr(self, "steam_p2p", None)
        if relay is not None:
            relay.forget_peer(peer)
    
    def _announce_player_left(self, connection, player) -> None:
        """Retail PLAYER_LEFT "{0} has disconnected" (packet 50).

        EN:596 sits right after PLAYER_JOINED and no stock client binary
        references it, so the retail server sent it; the stock PlayerLeft(64)
        handler prints nothing.  Same lane as PLAYER_JOINED, sent before
        PlayerLeft.  Skipped for bots, spectators, players that never
        reached the game, map-rollover (ERROR_MATCH_ENDED) reloads and
        server shutdown.
        """

        if getattr(self, "_stopping", False) or bool(getattr(player, "is_bot", False)):
            return
        if int(getattr(connection, "disconnect_reason", -1)) == 18:
            return
        try:
            team = int(getattr(player, "team", -1))
        except (TypeError, ValueError):
            return
        if team not in (TEAM1, TEAM2):
            return
        if not getattr(connection, "in_game", True):
            return
        from server.announcements import broadcast_localised_overlay

        try:
            broadcast_localised_overlay(
                self, "PLAYER_LEFT", (str(player.name),),
                localise_parameters=False,
            )
        except ValueError:
            logger.debug("PLAYER_LEFT skipped for %r", player.name)

    async def _on_receive_data(self, peer, data: bytes):
        """Handle a received raw datagram (pre-join / unbound peers)."""
        connection = self.connections.get(peer)
        if not connection:
            return

        # Let connection handle packet routing (includes decompression, decryption)
        await connection.on_receive(data)

    async def _drain_ingame_packets(self):
        """Process every in-game packet that arrived since the last tick.

        Runs at the start of each simulation tick: inputs that arrived
        before tick N are applied at tick N, deterministically.
        """
        if not self._pending_ingame_packets:
            return
        count = min(
            len(self._pending_ingame_packets),
            self.config.packet_drain_budget,
        )
        pending = [self._pending_ingame_packets.popleft() for _ in range(count)]
        counts = self._pending_packet_counts()
        for connection, _data in pending:
            key = id(connection)
            queued = counts.get(key, 0)
            if queued > 1:
                counts[key] = queued - 1
            else:
                counts.pop(key, None)
        for connection, data in pending:
            # The disconnect path normally purges these rows.  Recheck at the
            # consumption boundary after every await as well: an earlier
            # packet in this local batch can disconnect or replace the same
            # peer, leaving its FIFO tail outside the shared deque purge.
            connections = getattr(self, "connections", None)
            peer = getattr(connection, "peer", None)
            if connections is not None and connections.get(peer) is not connection:
                continue
            player = getattr(connection, "player", None)
            players = getattr(self, "players", None)
            if (
                players is not None
                and player is not None
                and players.get(getattr(player, "id", None)) is not player
            ):
                continue
            try:
                await connection.on_receive(data)
            except Exception as e:
                logger.error(f"Error processing in-game packet: {e}", exc_info=True)
    
    def get_connection(self, peer):
        """Get connection for a peer."""
        return self.connections.get(peer)
    
    def broadcast(self, data: bytes, exclude: Optional[Player] = None,
                  reliable: bool = True, gameplay: bool = True,
                  record_mutation: bool = True,
                  known_player_id: Optional[int] = None):
        """Send packet to all connected players.

        gameplay=True (default): only clients that are fully in-game receive
        it. A client still connecting / building the world / mid-GameScene-
        transition must NOT get gameplay events (CreatePlayer, KillAction,
        ChatMessage, ...) — that flood crashes the compiled client. Such
        clients are caught up via reveal_world_to on their first ClientData.
        Pass gameplay=False for packets that must reach every connection
        regardless of state. ``record_mutation=False`` is reserved for
        ephemeral packets that share a terrain packet id but must never be
        replayed to a MapSync joiner (for example Snowball Damage(37)).
        ``known_player_id`` restricts delivery to connections whose
        ``known_player_lives`` contains that id (packets the retail roster
        resolves by player id: SetScore, ExplodeCorpse, ChatMessage, ...).
        """
        if getattr(self, "_stopping", False):
            return
        if known_player_id is not None:
            self.broadcast_known_player_packet(
                data, known_player_id, reliable=reliable, exclude=exclude
            )
            return

        packet_id = data[0] if len(data) > 0 else -1
        # SetColor (11) is player palette state and is snapshotted separately
        # during reveal; replaying it after MapSync can restore a stale colour.
        if record_mutation and gameplay and packet_id in (7, 32, 33, 37, 40):
            self._record_map_mutation(data)
        if packet_id not in self.config.log_suppress_packets:
            logger.debug(f"SEND broadcast packet_id={packet_id} len={len(data)} to {len(self.connections)} clients")

        for connection in self.connections.values():
            if exclude and connection.player == exclude:
                continue
            if gameplay and not connection.in_game:
                continue
            connection.send(data, reliable=reliable)

    async def broadcast_message(
        self,
        message: str,
        chat_type: int | None = None,
    ) -> None:
        """Broadcast a plugin/server notice on the top-screen HUD lane.

        Plugin hooks are asynchronous, so this compatibility API remains an
        awaitable even though packet construction and ENet queueing are both
        synchronous and non-blocking. Connecting clients remain gameplay-
        gated to avoid native scene-transition crashes.

        ``chat_type`` remains as an explicit compatibility escape hatch.  A
        caller that supplies it receives the historical ChatMessage routing;
        the default is the retail ``CHAT_BIG`` overlay, never system chat.
        """

        if chat_type is None:
            from server.announcements import broadcast_overlay

            broadcast_overlay(self, message)
            return
        packet = ChatMessage()
        packet.player_id = 0xFF
        packet.chat_type = int(chat_type)
        packet.value = str(message)
        self.broadcast(bytes(packet.generate()))

    async def broadcast_localised_message(
        self,
        string_id: str,
        parameters=(),
        *,
        localise_parameters: bool = False,
        override_previous: bool = False,
    ) -> None:
        """Broadcast a retail-localized top-screen announcement."""

        from server.announcements import broadcast_localised_overlay

        broadcast_localised_overlay(
            self,
            string_id,
            parameters,
            localise_parameters=localise_parameters,
            override_previous=override_previous,
        )

    def mark_map_snapshot_complete(self, connection) -> int:
        """Bind a joining connection to the terrain sequence represented by
        the MapSync payload just serialized for it."""
        watermark = self._map_mutation_sequence
        connection.map_mutation_watermark = watermark
        connection.map_mutation_overflow = False
        if (
            hasattr(connection, "map_cell_watermark")
            and self._map_mutation_listener_token is not None
        ):
            connection.map_cell_watermark = self._map_cell_sequence
            connection.map_cell_overflow = False
        if hasattr(connection, "map_air_replay"):
            snapshot_air = getattr(
                self.world_manager, "snapshot_air_overrides", None
            )
            columns = (
                tuple(snapshot_air())
                if callable(snapshot_air)
                and bool(getattr(self.config, "map_air_catchup_enabled", False))
                else ()
            )
            if columns:
                cells = sum(bin(mask).count("1") for _x, _y, mask in columns)
                logger.info(
                    "Join air catch-up armed for %s: %d cells in %d columns",
                    getattr(getattr(connection, "peer", None), "address", "?"),
                    cells,
                    len(columns),
                )
            connection.map_air_replay = _MapAirReplayLease(columns=columns)
        self._prune_map_mutations()
        return watermark

    def replay_map_air_overrides(self, connection) -> bool:
        """Repair destroyed cells represented by this peer's MapSync.

        Runs on the gameplay thread only after the first ClientData proves the
        native GameScene exists. The snapshot is a compact set of per-column
        Z masks taken at the same boundary as the map mutation watermark.
        Cells changed later are read from canonical VXL at send time and are
        then reasserted by :meth:`replay_map_mutations`, preserving ordering.

        Returns ``True`` when the frozen snapshot is exhausted. ``False``
        keeps the connection gameplay-gated until its next input frame.
        """

        lease = getattr(connection, "map_air_replay", None)
        if lease is None:
            return True

        player = getattr(connection, "player", None)
        if player is None:
            raise RuntimeError(
                "pre-snapshot terrain repair requires an admitted player id"
            )
        actor_id = int(player.id)
        budget = max(
            1,
            int(getattr(self.config, "map_air_catchup_batch_limit", 256)),
        )
        sent = 0

        while sent < budget:
            if not lease.remaining_mask:
                if lease.column_index >= len(lease.columns):
                    connection.map_air_replay = None
                    return True
                x, y, mask = lease.columns[lease.column_index]
                lease.column_index += 1
                lease.current_x = int(x)
                lease.current_y = int(y)
                lease.remaining_mask = int(mask)
                if not lease.remaining_mask:
                    continue

            lowest_bit = lease.remaining_mask & -lease.remaining_mask
            z = lowest_bit.bit_length() - 1
            cell = (lease.current_x, lease.current_y, z)
            # 33 builds air; a PaintBlock pins a stale-colour solid (the
            # client ignores 33 on solid cells). Resending both is harmless.
            for data in self.terrain_repair.canonical_packets(cell, actor_id):
                # Advance only after ENet accepts the reliable packet. A failed
                # reveal retries the unsent bit on the next ClientData.
                connection.send(data, reliable=True)
            lease.remaining_mask ^= lowest_bit
            sent += 1

        if (
            not lease.remaining_mask
            and lease.column_index >= len(lease.columns)
        ):
            connection.map_air_replay = None
            return True
        return False

    def _bind_world_mutation_journal(self) -> None:
        """Subscribe catch-up journaling to the active canonical VXL.

        The callback runs synchronously after a successful world mutation on
        the gameplay thread. Match transitions replace ``world_manager`` and
        call this method again after installing the prepared map.
        """

        old_world = getattr(self, "_map_mutation_listener_world", None)
        old_token = getattr(self, "_map_mutation_listener_token", None)
        if old_world is not None and old_token is not None:
            unsubscribe = getattr(old_world, "unsubscribe_mutations", None)
            if callable(unsubscribe):
                unsubscribe(old_token)

        world = self.world_manager
        subscribe = getattr(world, "subscribe_mutations", None)
        self._map_mutation_listener_world = world
        self._map_mutation_listener_token = (
            subscribe(self._record_canonical_map_mutation)
            if callable(subscribe) else None
        )

    def _record_canonical_map_mutation(
        self,
        x: int,
        y: int,
        z: int,
        _solid: bool,
        _color: int,
        _topology_version: int,
    ) -> None:
        """Apply terrain-coupled entity lifecycles and retain join catch-up."""

        if not _solid:
            # Placement support is indexed by exact canonical voxel. This is
            # synchronous with the committed removal, so mines/turrets explode
            # and passive gadgets disappear before another simulation frame can
            # replicate them floating in air. Build one context only when that
            # support bucket is non-empty.
            cell = (int(x), int(y), int(z))
            if self.entity_registry.has_support_at(cell):
                self.entity_registry.support_removed(
                    cell,
                    self._build_entity_ctx(),
                )

        # This cursor is deliberately per cell, not WorldManager's per-batch
        # topology version. A collapse publishes many cells under one topology
        # version; a per-cell cursor lets retry resume after the exact packet
        # ENet accepted without replaying the successful prefix.
        self._map_cell_sequence += 1
        sequence = self._map_cell_sequence
        pending = any(
            not getattr(connection, "in_game", False)
            and getattr(connection, "map_cell_watermark", None) is not None
            for connection in self.connections.values()
        )
        if not pending:
            return
        self._map_cell_journal.append(
            (sequence, (int(x), int(y), int(z)))
        )
        self._enforce_map_cell_journal_limit()

    def _enforce_map_cell_journal_limit(self) -> None:
        """Bound exact-cell catch-up and reject joins missing any sequence."""

        limit = max(
            64,
            int(getattr(self.config, "max_map_mutation_journal", 8192)),
        )
        while len(self._map_cell_journal) > limit:
            dropped_sequence, _cell = self._map_cell_journal.popleft()
            for connection in self.connections.values():
                watermark = getattr(
                    connection, "map_cell_watermark", None
                )
                if (
                    getattr(connection, "in_game", False)
                    or watermark is None
                    or int(watermark) >= int(dropped_sequence)
                ):
                    continue
                if not getattr(connection, "map_cell_overflow", False):
                    connection.map_cell_overflow = True
                    self.metrics.map_mutation_overflows += 1

    def _record_map_mutation(self, data: bytes) -> None:
        self._map_mutation_sequence += 1
        pending = any(
            not getattr(connection, "in_game", False)
            and getattr(connection, "map_mutation_watermark", None) is not None
            and getattr(connection, "map_cell_watermark", None) is None
            for connection in self.connections.values()
        )
        if pending:
            self._map_mutation_journal.append(
                (self._map_mutation_sequence, bytes(data))
            )
            self._enforce_map_mutation_journal_limit()

    def _enforce_map_mutation_journal_limit(self) -> None:
        """Cap pending join catch-up while refusing unsafe partial replays."""
        limit = max(
            64,
            int(getattr(self.config, "max_map_mutation_journal", 8192)),
        )
        while len(self._map_mutation_journal) > limit:
            dropped_sequence, _data = self._map_mutation_journal.popleft()
            for connection in self.connections.values():
                watermark = getattr(connection, "map_mutation_watermark", None)
                if (
                    getattr(connection, "in_game", False)
                    or watermark is None
                    or getattr(connection, "map_cell_watermark", None)
                    is not None
                ):
                    continue
                if int(watermark) < int(dropped_sequence):
                    connection.map_mutation_overflow = True
                    self.metrics.map_mutation_overflows += 1

    def replay_map_mutations(self, connection) -> None:
        """Replay canonical terrain newer than this joiner's map snapshot.

        Real connections use exact committed cells. The native packet journal
        below remains only as a compatibility fallback for focused embedders
        without a WorldManager topology stream.
        """
        cell_watermark = getattr(
            connection, "map_cell_watermark", None
        )
        if cell_watermark is not None:
            self._replay_canonical_map_mutations(
                connection, int(cell_watermark)
            )
            return

        # Compatibility fallback for focused embedders that do not expose the
        # canonical WorldManager mutation stream.
        watermark = getattr(connection, "map_mutation_watermark", None)
        if watermark is None:
            return
        if getattr(connection, "map_mutation_overflow", False):
            self._fail_map_catchup(connection)
            return
        if (
            self._map_mutation_journal
            and self._map_mutation_journal[0][0] > int(watermark) + 1
        ):
            connection.map_mutation_overflow = True
            self.metrics.map_mutation_overflows += 1
            self._fail_map_catchup(connection)
            return
        for sequence, data in tuple(self._map_mutation_journal):
            if sequence > watermark:
                connection.send(data, reliable=True)
                # Advance only after a successful enqueue. If a later send
                # fails, first-ClientData retry resumes here without applying
                # an earlier Damage packet twice.
                watermark = sequence
                connection.map_mutation_watermark = sequence
        connection.map_mutation_watermark = self._map_mutation_sequence
        self._prune_map_mutations()

    def _replay_canonical_map_mutations(
        self, connection, watermark: int
    ) -> None:
        """Coalesce and replay final VXL state newer than ``watermark``.

        A coordinate can be built, painted, destroyed, and rebuilt while a
        client loads. Keeping only its last occurrence avoids duplicate native
        effects. Coordinates retain their first mutation order so a multi-cell
        build remains base-before-extension, while each packet reads the final
        VXL state. Collapsed structures are exact removals because WorldManager
        publishes every removed cell.
        """

        while True:
            if getattr(connection, "map_cell_overflow", False):
                self._fail_map_catchup(connection)
                return

            lease = getattr(connection, "map_cell_replay", None)
            if lease is None:
                target_sequence = self._map_cell_sequence
                journal = tuple(self._map_cell_journal)
                if journal and journal[0][0] > int(watermark) + 1:
                    connection.map_cell_overflow = True
                    self.metrics.map_mutation_overflows += 1
                    self._fail_map_catchup(connection)
                    return

                first_occurrence: dict[tuple[int, int, int], int] = {}
                for index, (sequence, cell) in enumerate(journal):
                    if int(watermark) < int(sequence) <= target_sequence:
                        # Keep the first-occurrence order so a supported build
                        # chain remains base-before-extension. The packet is
                        # generated later from the coordinate's final state.
                        first_occurrence.setdefault(cell, index)
                cells = tuple(
                    cell
                    for cell, _index in sorted(
                        first_occurrence.items(), key=lambda item: item[1]
                    )
                )
                lease = _MapCellReplayLease(
                    target_sequence=target_sequence,
                    cells=cells,
                )
                connection.map_cell_replay = lease

            cells = lease.cells
            player = getattr(connection, "player", None)
            if cells and player is None:
                raise RuntimeError(
                    "canonical terrain catch-up requires an admitted player id"
                )
            actor_id = int(player.id) if player is not None else 0
            while lease.next_index < len(cells):
                cell = cells[lease.next_index]
                for data in self.terrain_repair.canonical_packets(cell, actor_id):
                    connection.send(data, reliable=True)
                # Only advance after ENet accepted this reliable packet. A
                # failed reveal retries from the first unsent exact cell.
                lease.next_index += 1

            watermark = int(lease.target_sequence)
            connection.map_cell_watermark = watermark
            connection.map_mutation_watermark = self._map_mutation_sequence
            connection.map_cell_replay = None
            self._prune_map_mutations()
            if self._map_cell_sequence <= watermark:
                return

    def _fail_map_catchup(self, connection) -> None:
        """Reject a join whose terrain catch-up is no longer contiguous."""
        logger.warning(
            "Disconnecting %s: map mutation journal overflow during join",
            getattr(getattr(connection, "peer", None), "address", "<unknown>"),
        )
        disconnect = getattr(connection, "disconnect", None)
        if callable(disconnect):
            disconnect(reason=13)  # DISCONNECT.ERROR_DATA: safer than desync.
        raise RuntimeError(
            "map mutation journal overflow; reconnect required for a "
            "contiguous terrain snapshot"
        )

    def _prune_map_mutations(self) -> None:
        pending_watermarks = [
            int(connection.map_mutation_watermark)
            for connection in self.connections.values()
            if not getattr(connection, "in_game", False)
            and getattr(connection, "map_mutation_watermark", None) is not None
        ]
        if not pending_watermarks:
            self._map_mutation_journal.clear()
        else:
            oldest = min(pending_watermarks)
            while (self._map_mutation_journal
                   and self._map_mutation_journal[0][0] <= oldest):
                self._map_mutation_journal.popleft()

        cell_watermarks = [
            int(connection.map_cell_watermark)
            for connection in self.connections.values()
            if not getattr(connection, "in_game", False)
            and getattr(connection, "map_cell_watermark", None) is not None
        ]
        if not cell_watermarks:
            self._map_cell_journal.clear()
            return
        oldest_cell = min(cell_watermarks)
        while (
            self._map_cell_journal
            and self._map_cell_journal[0][0] <= oldest_cell
        ):
            self._map_cell_journal.popleft()
    
    def broadcast_team(self, team_id: int, data: bytes):
        """Send packet to all players on a team."""
        packet_id = data[0] if len(data) > 0 else -1
        if packet_id not in self.config.log_suppress_packets:
            logger.debug(f"SEND team={team_id} packet_id={packet_id} len={len(data)}")
        
        for connection in self.connections.values():
            if not connection.in_game:
                continue
            if connection.player and connection.player.team == team_id:
                connection.send(data)
