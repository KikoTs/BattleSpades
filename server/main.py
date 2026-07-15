"""
BattleSpades Main Server
Ace of Spades Protocol 1.0 Battle Builders

Uses ENet for networking with asyncio integration.
"""

import asyncio
from collections import deque
import logging
import sys
import time
from typing import Dict, Optional, TYPE_CHECKING

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

if TYPE_CHECKING:
    import enet

logger = logging.getLogger(__name__)

# Native Damage processing changes velocity before Character physics. Across
# six clean retail contacts, the matching authoritative pre-physics state was
# consistently the third ClientData accepted *after* impact; neither
# server.loop_count nor the sparse client loop label was a stable clock. This
# is not a transport ACK. The effect itself is recomputed at that state.
_SNOWBALL_PREDICTION_OBSERVED_FRAMES = 3


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
        # Active radar stations per owning team. TeamMapVisibility is sent
        # only to teammates and reference-counted for overlapping stations.
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
        configure_prefab_search_dirs(config.prefabs_path)
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
        from server.voting import VoteManager
        self.vote_manager = VoteManager(self)
        # In-game packets received since the last simulation tick; drained
        # synchronously at the start of each tick so an input that ARRIVED
        # before tick N is guaranteed to be APPLIED at tick N (dispatching
        # via create_task could slip past the tick — input timing became a
        # per-packet race no WorldUpdate stamp offset could compensate).
        self._pending_ingame_packets = deque()
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
        self.terrain_repair = TerrainRepairService(self)
        self.combat = CombatSystem(self)
        self.construction = ConstructionSafetyService(self)
        self.prefab_actions = PrefabActionService(self)
        self.deployable_actions = DeployableActionService(self)
        self.oriented_actions = OrientedActionService(self)
        self.debug_parity = DebugParityManager(self)
        
        # A2S Query handler for Steam browser and LAN discovery
        self.a2s_handler = A2SHandler(self)
        
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

        loaded = await load_external_plugins(
            self.plugin_manager,
            Path(self.config.plugins_path),
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

    async def _process_respawns(self) -> None:
        """Compatibility delegate to the round lifecycle service."""
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
        if tool == int(getattr(C, "SNOWBLOWER_TOOL", 29)):
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
        from server.projectiles import DrillContact, ProjectileDeployment
        for event in events:
            if isinstance(event, DrillContact):
                self._apply_drill_contact(event)
            elif isinstance(event, ProjectileDeployment):
                self._deploy_launched_mine(event)
            else:
                self._explode_projectile(event)

    def _apply_drill_contact(self, event) -> None:
        """Apply one measured 81-cell Drill bore and replicate it safely.

        A live type-10 packet is compact and drives the retail Drill sound,
        particles, and exact radius-2 BlockManager footprint.  It requires a
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

        positions = drill_contact_cells(event.block)
        destroyed = self.world_manager.destroy_blocks(positions)
        if not destroyed:
            return

        combat = get_combat_system(self)
        raw_entity_id = getattr(event.projectile, "entity_id", None)
        live_entity = (
            self.entity_registry.get(int(raw_entity_id))
            if raw_entity_id is not None
            else None
        )
        if live_entity is None:
            combat._broadcast_block_destroy(
                owner,
                destroyed,
                damage_type=int(C.WEAPON_DAMAGE),
                causer_id=int(owner.id),
            )
            return

        packet = Damage()
        packet.player_id = int(owner.id)
        packet.type = int(C.DRILL_DAMAGE)
        packet.damage = float(
            getattr(C, "DRILL_DRILLING_BLOCK_DAMAGE", 20.0)
        )
        packet.face = 0
        packet.chunk_check = 1
        packet.seed = 0
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
        combat.record_exact_block_destroy_catchup(
            owner,
            destroyed,
            causer_id=int(owner.id),
        )
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
        )
        ent = self.entity_registry.place(
            int(getattr(C, "LANDMINE_ENTITY", 9)),
            event.x, event.y, event.z,
            state=internal_team_to_wire(owner.team),
            kind="deployable", player_id=owner.id, behavior=behavior,
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

        # Projectiles that don't self-destroy blocks (RPG2, block_damage 2)
        # ACCUMULATE damage; grenade-family + strong warheads destroy outright.
        force_destroy = ex.spec.behavior != "contact"
        self._apply_blast(gx, gy, gz, ex.damage, ex.block_damage,
                          ex.spec.kill_type, thrower, crater_radius=1,
                          force_destroy=force_destroy,
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
        if not world.set_block(*position, True, color):
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

    def _apply_blast(self, gx, gy, gz, damage, block_damage, kill_type, thrower,
                     crater_radius: int = 1, force_destroy: bool = True,
                     blast_radius: float = 16.0, knockback_min: float = 0.0,
                     knockback_max: float = 0.0,
                     self_knockback_min=None, self_knockback_max=None,
                     prediction_frame_delay: int | None = None) -> None:
        """Shared explosion: crater a cube of `crater_radius` and damage nearby
        players with the live-verified falloff. Used by projectiles AND
        deployables (dynamite/landmine/C4)."""
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
        bx, by, bz = int(gx), int(gy), int(gz)
        r = max(1, int(crater_radius))
        positions = [
            (ax, ay, az)
            for ax in range(bx - r, bx + r + 1)
            for ay in range(by - r, by + r + 1)
            for az in range(bz - r, bz + r + 1)
        ]
        if getattr(self.config, "build_damage", True) and block_damage > 0.0:
            if block_damage >= DEFAULT_BLOCK_HEALTH or force_destroy:
                destroyed = self.world_manager.destroy_blocks(positions)
                if destroyed:
                    get_combat_system(self)._broadcast_block_destroy(
                        thrower if thrower is not None else None, destroyed
                    )
            else:
                combat = get_combat_system(self)
                for block in positions:
                    if self.world_manager.get_solid(*block):
                        combat._apply_block_damage(thrower, block, block_damage)

        # Player blast damage: within 16 blocks, LOS-gated. Same falloff CURVE
        # as the live-verified grenade (min(100, 4096/sq)), scaled to each
        # warhead's max damage.
        scale = damage / 100.0
        for target in list(self.players.values()):
            if not target.alive or not target.spawned:
                continue
            dx = target.x - gx
            dy = target.y - gy
            dz = target.z - gz
            sq = dx * dx + dy * dy + dz * dz
            if sq >= float(blast_radius) ** 2:
                continue
            if self._blocked_los(gx, gy, gz, target.x, target.y, target.z):
                continue
            from server.explosions import explosion_impulse
            target_knockback_min = float(knockback_min)
            target_knockback_max = float(knockback_max)
            if target is thrower:
                if self_knockback_min is not None:
                    target_knockback_min = float(self_knockback_min)
                if self_knockback_max is not None:
                    target_knockback_max = float(self_knockback_max)
            crouched = bool(getattr(getattr(target, "input", None), "crouch", False))
            impulse_preview = explosion_impulse(
                (gx, gy, gz), target.position, blast_radius,
                target_knockback_min, target_knockback_max,
                crouched=crouched,
            )
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
            dmg = int(round(damage if sq <= 1.0 else min(damage, (4096.0 / sq) * scale)))
            if dmg > 0:
                target.damage(dmg, source=thrower, kill_type=int(kill_type))

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
            ex, ey, ez = behavior.get_hit_center(entity)
            dx, dy, dz = ex - gx, ey - gy, ez - gz
            sq = dx * dx + dy * dy + dz * dz
            if sq >= float(blast_radius) ** 2:
                continue
            if self._blocked_los(gx, gy, gz, ex, ey, ez):
                continue
            entity_damage = (
                float(damage)
                if sq <= 1.0
                else min(float(damage), (4096.0 / sq) * scale)
            )
            if entity_damage > 0.0:
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
        from shared.packet import SetColor
        color = SetColor()
        color.player_id = player.id
        color.value = int(player.block_color) & 0xFFFFFF
        self.broadcast(bytes(color.generate()))

    def reveal_world_to(self, connection) -> None:
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
        from server.roster import catch_up_roster
        catch_up_roster(self, connection)

        # BlockLine/BlockBuild packets carry no RGB. Refresh every sender's
        # current palette before replaying terrain so late joiners render the
        # authoritative VXL colours.
        from shared.packet import SetColor
        for roster_player in self.players.values():
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

        # World ambience + the in-game music bed for this now-settled client
        # (a mid-round joiner must get both directly — the round-start
        # broadcast already fired before they arrived). play_music_to sends
        # StopMusic+PlayMusic (needed to clear the client's leftover menu music).
        try:
            import random
            from server.audio import send_map_ambient, play_music_to, \
                GAMEPLAY_TRACKS
            player = getattr(connection, "player", None)
            if player is not None:
                send_map_ambient(self, player)
            play_music_to(connection, random.choice(GAMEPLAY_TRACKS))
        except Exception:
            logger.debug("reveal ambient/music send failed", exc_info=True)

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
            from shared.packet import CreateEntity
            for ent in self.entity_registry.static_entities():
                try:
                    pkt = CreateEntity()
                    pkt.set_entity(ent.to_wire_entity())
                    connection.send(bytes(pkt.generate()), reliable=True)
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

        player = getattr(connection, "player", None)
        if player is not None and self._radar_station_counts.get(player.team, 0) > 0:
            self._send_radar_visibility(player, True)

    def _send_radar_visibility(self, player, visible: bool) -> None:
        """Expose the enemy team on one teammate's minimap."""
        from shared.packet import TeamMapVisibility
        enemy_team = TEAM2 if player.team == TEAM1 else TEAM1
        packet = TeamMapVisibility()
        packet.team_id = int(enemy_team)
        packet.visible = int(bool(visible))
        player.send(bytes(packet.generate()), reliable=True)

    def _radar_station_added(self, team: int) -> None:
        team = int(team)
        count = int(self._radar_station_counts.get(team, 0)) + 1
        self._radar_station_counts[team] = count
        if count == 1:
            for player in self.players.values():
                if player.team == team:
                    self._send_radar_visibility(player, True)

    def _radar_station_removed(self, team: int) -> None:
        team = int(team)
        count = max(0, int(self._radar_station_counts.get(team, 0)) - 1)
        self._radar_station_counts[team] = count
        if count == 0:
            for player in self.players.values():
                if player.team == team:
                    self._send_radar_visibility(player, False)

    def broadcast_create_entity(self, map_entity) -> None:
        """Announce a placed entity (crate/intel/...) to all clients."""
        if not getattr(map_entity, "wire_visible", True):
            # Legacy objective markers can exist for authoritative mode logic
            # without being legal packet-21 entities in the retail client.
            return
        from shared.packet import CreateEntity
        pkt = CreateEntity()
        pkt.set_entity(map_entity.to_wire_entity())
        self.broadcast(bytes(pkt.generate()), reliable=True)

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
        from shared.packet import DestroyEntity
        pkt = DestroyEntity()
        pkt.entity_id = int(entity_id)
        self.broadcast(bytes(pkt.generate()), reliable=True)

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

    def broadcast_set_score(self, team) -> None:
        """Update one team's HUD score on every in-game client via the
        lightweight SetScore(85) packet — the correct mid-game score update.
        Unlike StateData it carries no scene/prefab/UGC data, so the client
        just sets team.score and redraws the HUD (no re-init, no crash)."""
        from shared.packet import SetScore
        from server.connection import internal_team_to_wire
        from shared.constants import SCORE, SCORE_REASON

        pkt = SetScore()
        pkt.type = int(SCORE.TEAM)
        pkt.reason = int(SCORE_REASON.KILL_SCORE_REASON)
        pkt.specifier = internal_team_to_wire(team.id)
        pkt.value = int(team.score)
        self.broadcast(bytes(pkt.generate()))

    async def start(self):
        """Initialize and start the server."""
        import enet

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
        
        # Create ENet host - matching reference pattern
        address = enet.Address(b"", self.config.port)
        self.host = enet.Host(
            address,
            peerCount=self.config.max_connections,
            channelLimit=1,  # Reference uses 1 channel
            incomingBandwidth=0,
            outgoingBandwidth=0,
        )
        
        # Enable compression like reference
        self.host.compress_with_range_coder()
        
        # Set intercept for A2S queries (reference pattern)
        self.host.intercept = self._intercept
        logger.info("A2S/LAN intercept registered")
        
        # Load map
        self.world_manager.load_map(self.config.map_name)
        
        # Initialize game mode
        from modes import get_mode_class
        mode_class = get_mode_class(self.config.game_mode)
        if mode_class:
            self.mode = mode_class(self)
            await self.mode.on_mode_start()

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
    
    async def stop(self):
        """Stop the server."""
        if not self.running:
            return
        
        logger.info("Stopping server...")
        self.running = False

        if self.bots is not None:
            await self.bots.close()
            self.bots = None
        
        if self.mode:
            await self.mode.on_mode_end()

        if self.debug_parity is not None:
            self.debug_parity.close()
        
        # Disconnect all players
        for peer in list(self.connections.keys()):
            peer.disconnect()
        
        if self.host:
            self.host.flush()
            self.host = None
        
        logger.info("Server stopped")
    
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
                        if len(self._pending_ingame_packets) < self.config.max_pending_packets:
                            self._pending_ingame_packets.append((connection, data))
                        else:
                            self._dropped_ingame_packets += 1
                            self.metrics.dropped_ingame_packets += 1
                    else:
                        # Pre-join flows (handshake, map transfer) can be
                        # slow — keep them off the simulation path.
                        asyncio.create_task(self._on_receive_data(peer, data))
                    
            except Exception as e:
                logger.error(f"Error in net_update: {e}", exc_info=True)
    
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

        # Reject banned IPs before we allocate any state for them.
        from server.bans import address_host
        ban = self.ban_manager.is_banned(address_host(peer))
        if ban is not None:
            logger.info("Rejected banned client %s (%s)", peer.address, ban.get("reason"))
            try:
                peer.disconnect(1)  # DISCONNECT_BANNED
            except Exception:
                pass
            return

        # Check if connection already exists
        connection = self.connections.get(peer)
        if connection is None:
            connection = Connection(peer, self)
            self.connections[peer] = connection
        
        # Call connection's on_connect
        connection.on_connect(data)
    
    def _on_disconnect_sync(self, peer):
        """Handle disconnection (sync version for net_update)."""
        connection = self.connections.pop(peer, None)
        if not connection:
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

            # Mode state (VIP ownership, CTF intel, etc.) must observe the
            # departing identity. The event is drained next tick after the
            # player has been removed from team/player collections.
            if self.mode is not None:
                self.queue_mode_event("on_player_leave", player)

            # Numeric player ids are reused from the lowest free slot. Retire
            # every owner-sensitive producer/cache before exposing this id to
            # another connection; RoundLifecycle preserves ordinary world
            # construction while removing deployables and stale credit.
            self.round_lifecycle.forget_player(player)
            
            # Remove from team
            if player.team in self.teams:
                self.teams[player.team].remove_player(player)
            
            # Remove from players
            self.players.pop(player.id, None)
            
            # Broadcast disconnect
            left_packet = PlayerLeft()
            left_packet.player_id = player.id
            self.broadcast(bytes(left_packet.generate()))
        
        connection.on_disconnect()
    
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
                  record_mutation: bool = True):
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
        """
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
        chat_type: int = CHAT_SYSTEM,
    ) -> None:
        """Broadcast a plugin/system chat notice on the gameplay thread.

        Plugin hooks are asynchronous, so this compatibility API remains an
        awaitable even though packet construction and ENet queueing are both
        synchronous and non-blocking. Connecting clients remain gameplay-
        gated to avoid native scene-transition crashes.
        """

        packet = ChatMessage()
        packet.player_id = 0xFF
        packet.chat_type = int(chat_type)
        packet.value = str(message)
        self.broadcast(bytes(packet.generate()))

    def mark_map_snapshot_complete(self, connection) -> int:
        """Bind a joining connection to the terrain sequence represented by
        the MapSync payload just serialized for it."""
        watermark = self._map_mutation_sequence
        connection.map_mutation_watermark = watermark
        connection.map_mutation_overflow = False
        self._prune_map_mutations()
        return watermark

    def _record_map_mutation(self, data: bytes) -> None:
        self._map_mutation_sequence += 1
        pending = any(
            not getattr(connection, "in_game", False)
            and getattr(connection, "map_mutation_watermark", None) is not None
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
                if getattr(connection, "in_game", False) or watermark is None:
                    continue
                if int(watermark) < int(dropped_sequence):
                    connection.map_mutation_overflow = True
                    self.metrics.map_mutation_overflows += 1

    def replay_map_mutations(self, connection) -> None:
        """Replay every terrain packet newer than this joiner's snapshot.

        Native BlockBuild/BlockLine/Damage packets are replayed rather than a
        synthetic cell diff so client collapse, colours, and effects follow
        the same code paths as clients that were already in game.
        """
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
            return
        oldest = min(pending_watermarks)
        while (self._map_mutation_journal
               and self._map_mutation_journal[0][0] <= oldest):
            self._map_mutation_journal.popleft()
    
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
