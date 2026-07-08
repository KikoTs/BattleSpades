"""
BattleSpades Main Server
Ace of Spades Protocol 1.0 Battle Builders

Uses ENet for networking with asyncio integration.
"""

import asyncio
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
from .game_constants import TEAM1, TEAM2, MAX_HEALTH, DEFAULT_BLOCK_HEALTH
from .player import Player, set_movement_authority, INPUT_DELAY_TICKS
from .team import Team
from .world_manager import WorldManager
from .connection import Connection
from .a2s_query import A2SHandler
from .debug_parity import DebugParityManager

if TYPE_CHECKING:
    import enet

logger = logging.getLogger(__name__)


class BattleSpadesServer:
    """
    Main server class for Battle Builders.
    Manages ENet networking, players, world state, and game logic.
    """
    
    def __init__(self, config: ServerConfig):
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
        # Plugin system: loaded at startup, fired at the mode-event + tick
        # dispatch points below. Drop a *.py with a BasePlugin subclass in
        # plugins/ and it's auto-discovered.
        from plugins.base_plugin import PluginManager
        self.plugin_manager = PluginManager(self)
        # Persistent ban list (bans.json), enforced on connect.
        from server.bans import BanManager
        self.ban_manager = BanManager()
        # In-flight thrown grenades (server-authoritative blast). Each is a
        # dict: {x,y,z, vx,vy,vz, explode_at, thrower_id}.
        from server.projectiles import ProjectileEngine
        self.projectile_engine = ProjectileEngine()
        from server.voting import VoteManager
        self.vote_manager = VoteManager(self)
        # In-game packets received since the last simulation tick; drained
        # synchronously at the start of each tick so an input that ARRIVED
        # before tick N is guaranteed to be APPLIED at tick N (dispatching
        # via create_task could slip past the tick — input timing became a
        # per-packet race no WorldUpdate stamp offset could compensate).
        self._pending_ingame_packets: list = []
        # Game-logic events (kills, deaths, spawns, block edits, team changes)
        # queued SYNCHRONOUSLY from the sim/combat/packet paths and drained
        # once per tick in _game_loop. This is the SINGLE place mode hooks
        # fire — never asyncio.create_task from a sync path (it would slip
        # past the tick and reintroduce the input-timing race main.py warns
        # about above). (name, args) tuples.
        self._mode_events: list = []

        # Teams
        self.teams = {
            TEAM1: Team(TEAM1, config.team1_name, config.team1_color),
            TEAM2: Team(TEAM2, config.team2_name, config.team2_color),
        }
        
        # World
        self.world_manager = WorldManager(config)
        self.combat = CombatSystem(self)
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
        """Discover and load every BasePlugin subclass in the plugins/ package
        (any *.py except the base/dunder files). Failures are logged, never
        fatal — a bad plugin can't take the server down."""
        import importlib
        import inspect
        import pkgutil
        import plugins as plugins_pkg
        from plugins.base_plugin import BasePlugin

        loaded = 0
        for mod_info in pkgutil.iter_modules(plugins_pkg.__path__):
            if mod_info.name in ("base_plugin",) or mod_info.name.startswith("_"):
                continue
            try:
                module = importlib.import_module(f"plugins.{mod_info.name}")
            except Exception:
                logger.error("Failed to import plugin module %s", mod_info.name, exc_info=True)
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BasePlugin) and obj is not BasePlugin and obj.__module__ == module.__name__:
                    if await self.plugin_manager.load_plugin(obj):
                        loaded += 1
        if loaded:
            logger.info("Loaded %d plugin(s)", loaded)

    def queue_mode_event(self, name: str, *args) -> None:
        """Queue a game-logic event for the active mode. Drained once per tick
        in _game_loop (after on_tick). Safe to call from synchronous code
        (Player.die, combat, packet handlers) — never schedules a task."""
        self._mode_events.append((name, args))

    async def _process_respawns(self) -> None:
        """Respawn players whose death timer has elapsed. Mode-agnostic — CTF
        and TDM both rely on it (there was previously NO server respawn, so a
        killed player stayed dead forever). Reuses the calibrated spawn path
        (world_manager.get_spawn_point + Player.spawn) and re-broadcasts a
        CreatePlayer so every client re-renders the body alive at the new
        position."""
        if not self.players:
            return
        now = time.time()
        respawn_time = float(self.config.respawn_time)
        for player in list(self.players.values()):
            if player.alive or player.spawned or player.death_time <= 0.0:
                continue
            if now - player.death_time < respawn_time:
                continue
            # Remove this player's grave (spawned in Player.die) before the
            # body reappears.
            grave_id = getattr(player, '_grave_entity_id', None)
            if grave_id is not None:
                if self.entity_registry.remove(grave_id) is not None:
                    self.broadcast_destroy_entity(grave_id)
                player._grave_entity_id = None

            # Apply a mid-game class/loadout change (SetClassLoadout while
            # alive) at the respawn boundary — original-server semantics.
            pending_class = getattr(player, 'pending_class_id', None)
            if pending_class is not None:
                player.class_id = int(pending_class)
                player.pending_class_id = None
            pending_loadout = getattr(player, 'pending_loadout', None)
            if pending_loadout is not None:
                player.loadout = list(pending_loadout)
                player.pending_loadout = None

            self.respawn_player(player)

    def respawn_player(self, player) -> None:
        """Spawn (or re-spawn) a player at their team spawn and re-render the
        live body on every client. Shared by the death-timer respawn loop and
        the end-of-round restart."""
        spawn = self.world_manager.get_spawn_point(player.team)
        player.spawn(spawn[0], spawn[1], spawn[2])
        player.death_time = 0.0
        self._broadcast_create_player(player, spawn)
        # Refill the respawned client's tool counters (grenades included).
        player.restock_ammo()
        if self.mode is not None:
            self.queue_mode_event('on_player_spawn', player)

    def spawn_grenade(self, player, packet) -> None:
        """A player used an oriented item (grenade family, RPG/RPG2 rocket,
        drill, snowball, sticky/chemical). Register the server-authoritative
        projectile + rebroadcast the packet so every other client renders and
        simulates it locally (arc/flight + explosion FX + sound)."""
        from server.projectiles import PROJECTILE_SPECS
        tool = int(getattr(packet, "tool", 0))
        if tool not in PROJECTILE_SPECS:
            return  # not a projectile tool (deployables ride Place* packets)

        pos = getattr(packet, "position", None)
        vel = getattr(packet, "velocity", None)
        if not pos or not vel:
            return
        # Reject NaN/inf (a bad float can wedge the sim).
        vals = list(pos) + list(vel) + [getattr(packet, "value", 0.0)]
        if any(v != v or abs(v) > 1e6 for v in vals):
            return

        fuse = max(0.0, min(float(getattr(packet, "value", 3.0)), 10.0))

        p = self.projectile_engine.spawn(tool, pos, vel, fuse, player.id)
        if p is None:
            return

        if p.spec.entity_type:
            # Rocket/drill/snowball/molotov: NEITHER the firer nor other clients
            # spawn these locally (the client's send_rocket sends no local
            # entity — measured 2026-07-07). The server owns the flying entity:
            # spawn a CreateEntity of the right ENTITY type carrying the initial
            # pos+velocity so EVERY client renders + simulates the projectile,
            # and DestroyEntity on explosion plays the blast FX (see _explode).
            from server.connection import internal_team_to_wire
            state = internal_team_to_wire(player.team)
            ent = self.entity_registry.place(
                int(p.spec.entity_type),
                float(pos[0]), float(pos[1]), float(pos[2]),
                state=state, kind="projectile", player_id=player.id,
                vel=(float(vel[0]), float(vel[1]), float(vel[2])),
                radius=0.02,
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

        logger.info("PROJECTILE %s by %s tool=%d pos=(%.1f,%.1f,%.1f) fuse=%.2f eid=%d",
                    p.spec.name, player.name, tool, pos[0], pos[1], pos[2], fuse, p.entity_id)

    # Projectile physics lives in server/projectiles.py (the grenade math is
    # the verified port of the compiled client's mover sub_10011E90; rockets/
    # drill/snowball fly per the client's extracted flight constants).

    def _update_grenades(self, dt: float) -> None:
        """Advance all in-flight projectiles; apply any explosions."""
        explosions = self.projectile_engine.update(dt, self.world_manager)
        for ex in explosions:
            self._explode_projectile(ex)

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
        eid = int(getattr(ex, "entity_id", 0) or 0)
        if eid:
            if self.entity_registry.remove(eid) is not None:
                self.broadcast_destroy_entity(eid)

        # Projectiles that don't self-destroy blocks (RPG2, block_damage 2)
        # ACCUMULATE damage; grenade-family + strong warheads destroy outright.
        force_destroy = ex.spec.behavior != "contact"
        self._apply_blast(gx, gy, gz, ex.damage, ex.block_damage,
                          ex.spec.kill_type, thrower, crater_radius=1,
                          force_destroy=force_destroy)

    def _apply_blast(self, gx, gy, gz, damage, block_damage, kill_type, thrower,
                     crater_radius: int = 1, force_destroy: bool = True) -> None:
        """Shared explosion: crater a cube of `crater_radius` and damage nearby
        players with the live-verified falloff. Used by projectiles AND
        deployables (dynamite/landmine/C4)."""
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
            if sq >= 256.0:  # 16 blocks
                continue
            if self._blocked_los(gx, gy, gz, target.x, target.y, target.z):
                continue
            dmg = int(round(damage if sq <= 1.0 else min(damage, (4096.0 / sq) * scale)))
            if dmg > 0:
                target.damage(dmg, source=thrower, kill_type=int(kill_type))

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

    def reveal_world_to(self, connection) -> None:
        """Send a now-in-game client the map entities (crates). Called from the
        connection's FIRST ClientData, never during the join handshake — a
        flood of entity creates while the client is still building the world /
        mid-GameScene-transition crashes the compiled client natively.

        The player ROSTER is NOT sent here: existing players are already
        delivered in the handshake via ExistingPlayer (send_existing_players),
        and new joiners arrive via the gated CreatePlayer broadcast. Re-sending
        them here would duplicate-create them on the client.

        After this runs, connection.in_game is True so the ongoing gameplay
        broadcast stream (kills, respawns, scores, WorldUpdate) flows normally.
        """
        if not getattr(self.config, "entities_wire_ready", False):
            return
        from shared.packet import CreateEntity
        for ent in self.entity_registry.static_entities():
            try:
                pkt = CreateEntity()
                pkt.set_entity(ent.to_wire_entity())
                connection.send(bytes(pkt.generate()), reliable=True)
            except Exception:
                logger.debug("reveal entity send failed", exc_info=True)

    def broadcast_create_entity(self, map_entity) -> None:
        """Announce a placed entity (crate/intel/...) to all clients."""
        from shared.packet import CreateEntity
        pkt = CreateEntity()
        pkt.set_entity(map_entity.to_wire_entity())
        self.broadcast(bytes(pkt.generate()), reliable=True)

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

        state = build_state_data(self)
        data = bytes(state.generate())
        for connection in list(self.connections.values()):
            if not connection.in_game:
                continue  # mid-transition; caught up on first ClientData
            try:
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
            peerCount=self.config.max_players,
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

        # Dev bots: spawn after the mode is up so they join the active match.
        bot_count = int(getattr(self.config, "bot_count", 0) or 0)
        if bot_count > 0:
            from server.bots import BotManager
            self.bots = BotManager(self)
            self.bots.spawn_initial(bot_count)
            logger.info("Spawned %d dev bot(s)", bot_count)

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
        
        while True:
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
                        self._pending_ingame_packets.append((connection, data))
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
        """Main game tick loop: fixed-dt steps with wall-clock accumulation.

        Physics must advance by exactly tick_interval per step (the client
        engine integrates with the same fixed dt). The accumulator runs
        catch-up steps after a stall instead of silently dropping time,
        capped to avoid spiral-of-death after long hitches.
        """
        max_catch_up_steps = 5
        accumulator = 0.0
        last_time = time.perf_counter()
        # Tick health stats: a 60Hz simulation has a 16.7ms budget; any
        # blocking work on this thread (sync I/O, slow logging) shows up
        # here as slow ticks => movement lag bursts for every client.
        stat_ticks = 0
        stat_slow = 0
        stat_max_ms = 0.0
        stat_sum_ms = 0.0

        while self.running:
            current = time.perf_counter()
            accumulator += current - last_time
            last_time = current

            if accumulator > self.tick_interval * max_catch_up_steps:
                # Long stall (debugger, map load): drop the excess time.
                accumulator = self.tick_interval * max_catch_up_steps

            ticked = False
            while accumulator >= self.tick_interval:
                accumulator -= self.tick_interval
                self.loop_count += 1
                ticked = True
                tick_start = time.perf_counter()

                # Apply everything that arrived since the previous tick
                # BEFORE simulating — with the client clock one tick ahead
                # (clock_sync_loop_bias) the input stamped N is already
                # buffered here when tick N simulates.
                await self._drain_ingame_packets()

                # Simulate with inputs INPUT_DELAY_TICKS old: the client's
                # ClientData for frame N arrives 1-2 ticks after our tick N
                # passed (clocks are ClockSync-aligned), so the delayed tick
                # is the newest one whose input has reliably arrived.
                # Drive bot AI BEFORE the sim loop so the shared
                # simulate_tick steps each bot with the inputs the AI just
                # chose (bots are normal Players in self.players — no separate
                # physics step, no double-step).
                if self.bots is not None:
                    self.bots.update(self.tick_interval)

                for player in self.players.values():
                    await player.simulate_tick(self.tick_interval)

                self.a2s_handler.update()

                if self.mode:
                    await self.mode.on_tick(self.loop_count)
                    # Drain game-logic events queued this tick (kills, deaths,
                    # spawns, ...) into the mode, in order, off the sync death
                    # path. Snapshot + clear first so a handler that queues a
                    # new event defers it to the next tick.
                    if self._mode_events:
                        events = self._mode_events
                        self._mode_events = []
                        for name, args in events:
                            handler = getattr(self.mode, name, None)
                            if handler is not None:
                                await handler(*args)
                            # Fan the same event out to plugins (they hook the
                            # same names: on_player_spawn/kill/join/leave/...).
                            await self.plugin_manager.call_event(name, *args)
                # Per-tick plugin hook.
                await self.plugin_manager.call_event('on_tick', self.loop_count)

                # Respawn scheduler (mode-agnostic): bring dead players back
                # after config.respawn_time. snapshot the list — spawn()
                # doesn't mutate self.players but a handler might.
                await self._process_respawns()

                # Entity behaviors: crate pickups + respawn, and any future
                # tickable entities (deployables, capture points, ...).
                self.entity_registry.tick(self._build_entity_ctx())

                # In-flight grenade fuses / detonation.
                self._update_grenades(self.tick_interval)

                # HUD round-timer countdown (DisplayCountdown 84) — seconds
                # REMAINING, sent ~once/sec (not every frame; it's a whole
                # second display and 60Hz would flood the reliable channel).
                if (self.mode is not None and self.mode.started
                        and not self.mode.ended
                        and getattr(self.mode, "time_limit", 0) > 0
                        and self.loop_count % self.tick_rate == 0):
                    from server.scoreboard import send_round_timer
                    remaining = self.mode.time_limit - self.mode.elapsed_time
                    send_round_timer(self, remaining)

                # Vote-kick timeout (auto-fail after VOTE_DURATION).
                if self.vote_manager.active and self.loop_count % self.tick_rate == 0:
                    self.vote_manager.tick(time.time())

                tick_ms = (time.perf_counter() - tick_start) * 1000.0
                stat_ticks += 1
                stat_sum_ms += tick_ms
                if tick_ms > stat_max_ms:
                    stat_max_ms = tick_ms
                if tick_ms > 10.0:
                    stat_slow += 1
                if stat_ticks >= 600:  # one line every ~10s
                    logger.info(
                        "tick stats: avg=%.2fms max=%.2fms slow(>10ms)=%d/%d",
                        stat_sum_ms / stat_ticks, stat_max_ms, stat_slow, stat_ticks,
                    )
                    stat_ticks = stat_slow = 0
                    stat_max_ms = stat_sum_ms = 0.0

            # Send WorldUpdates immediately after simulating so the
            # positions in the packet are exactly the state of loop_count.
            #
            # UNRELIABLE on purpose: a WorldUpdate is superseded 16ms later,
            # while 60Hz reliable packets on ENet's single channel cause
            # ACK head-of-line blocking — the send queue backs up and
            # clients see movement in stale multi-second bursts.
            #
            # SELF-ROWS (worldupdate_include_self=True, original behavior):
            # the client reconciles its own row against its movement history
            # at the packet's loop_count (replay past POSITION_TOLERANCE,
            # snap past POSITION_RESET_TOLERANCE) — with parity physics the
            # diff stays under tolerance and the row only refreshes the
            # client's network anchor. Without self-rows that anchor stays
            # at the CreatePlayer spawn forever and the client engine snaps
            # the player back to it on jump. One serialized packet serves
            # every connection.
            #
            # Legacy exclusion mode (False) kept for A/B: per-connection
            # packets omitting the recipient's own row (pure prediction
            # locally; earlier sessions measured self-echo as chunky while
            # the worlds were still mismatched).
            if ticked and self.connections and self.config.broadcast_world_updates:
                include_self = (
                    self.config.worldupdate_include_self
                    and self.loop_count
                    % self.config.worldupdate_self_row_interval == 0
                )
                offset = self.config.worldupdate_loop_offset
                for connection in list(self.connections.values()):
                    if not connection.in_game:
                        continue  # not in GameScene yet — no WorldUpdate flood
                    player = connection.player
                    if (
                        include_self
                        and player is not None
                        and player.last_applied_input_loop is not None
                    ):
                        # Per-recipient stamp: the input tick the sim
                        # actually consumed for THIS player. Latency- and
                        # jitter-proof pairing with the client's history.
                        stamp = player.last_applied_input_loop + offset
                        data = self.build_world_update_data(
                            loop_count_override=stamp,
                        )
                        if self.config.debug_selfrow:
                            self._log_selfrow(player, stamp)
                    else:
                        # No input consumed yet (or self-rows disabled):
                        # never send a self-row the client could mispair.
                        exclude_id = player.id if player is not None else None
                        data = self.build_world_update_data(
                            exclude_player_id=exclude_id,
                        )
                    connection.send(data, reliable=False)

            await asyncio.sleep(0.001)
    
    def _log_selfrow(self, player, stamp: int) -> None:
        """Append one self-row sample (the stamp + position actually sent to
        this player) for offline reconciliation calibration against the
        client's per-frame capture. See tmp/reconcile_sim.py."""
        handle = getattr(self, "_selfrow_handle", None)
        if handle is None:
            import os
            os.makedirs("logs", exist_ok=True)
            handle = open("logs/selfrow_samples.ndjson", "w", encoding="utf-8")
            self._selfrow_handle = handle
        handle.write(
            '{"server_tick": %d, "stamp": %d, "input_loop": %d, '
            '"x": %.5f, "y": %.5f, "z": %.5f}\n'
            % (self.loop_count, stamp, player.last_applied_input_loop,
               player.x, player.y, player.z)
        )
        handle.flush()

    async def _world_update_loop(self):
        """Send world updates to clients at the authoritative server tick rate."""
        update_interval = self.tick_interval

        while self.running:
            data = self.build_world_update_data()
            if self.connections:
                self.broadcast(data)

            await asyncio.sleep(update_interval)

    def build_world_update_packet(
        self,
        exclude_player_id: Optional[int] = None,
        loop_count_override: Optional[int] = None,
    ) -> WorldUpdate:
        """Build the current WorldUpdate snapshot for all active players.

        exclude_player_id omits that player's own row — used so a client
        never receives (and never corrects to) its own server-side state.
        loop_count_override stamps the packet with the RECIPIENT's own
        last-consumed input loop (per-connection): the client reconciles
        its self-row against its movement history at this stamp, and only
        the actually-consumed input tick pairs correctly at any latency.
        """
        world_update = WorldUpdate()
        if loop_count_override is not None:
            world_update.loop_count = max(0, loop_count_override)
        else:
            # Fallback stamp for packets without a recipient-specific input
            # tick (no player / no input yet): the inputs of this (delayed)
            # tick. Only correct when transit latency ~= one tick.
            world_update.loop_count = max(
                0,
                self.loop_count - INPUT_DELAY_TICKS
                + self.config.worldupdate_loop_offset,
            )

        for pid, player in self.players.items():
            if pid == exclude_player_id:
                continue
            if not player.alive or not player.spawned:
                continue
            world_update[pid] = player.world_update_snapshot()

        world_update.updated_entities = list(self.entities.values())
        world_update.rocket_turrets = list(self.rocket_turrets.values())
        return world_update

    def build_world_update_data(
        self,
        exclude_player_id: Optional[int] = None,
        loop_count_override: Optional[int] = None,
    ) -> bytes:
        """Serialize the current WorldUpdate packet."""
        return bytes(
            self.build_world_update_packet(
                exclude_player_id, loop_count_override
            ).generate()
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

        # Release an id promised to a client that never finished joining.
        reserved = getattr(connection, "reserved_player_id", None)
        if reserved is not None:
            self.reserved_player_ids.discard(reserved)
        
        if connection.player:
            player = connection.player
            logger.info(f"Player {player.name} disconnected")
            
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
        pending = self._pending_ingame_packets
        self._pending_ingame_packets = []
        for connection, data in pending:
            try:
                await connection.on_receive(data)
            except Exception as e:
                logger.error(f"Error processing in-game packet: {e}", exc_info=True)
    
    def get_connection(self, peer):
        """Get connection for a peer."""
        return self.connections.get(peer)
    
    def broadcast(self, data: bytes, exclude: Optional[Player] = None,
                  reliable: bool = True, gameplay: bool = True):
        """Send packet to all connected players.

        gameplay=True (default): only clients that are fully in-game receive
        it. A client still connecting / building the world / mid-GameScene-
        transition must NOT get gameplay events (CreatePlayer, KillAction,
        ChatMessage, ...) — that flood crashes the compiled client. Such
        clients are caught up via reveal_world_to on their first ClientData.
        Pass gameplay=False for packets that must reach every connection
        regardless of state.
        """
        packet_id = data[0] if len(data) > 0 else -1
        if packet_id not in self.config.log_suppress_packets:
            logger.debug(f"SEND broadcast packet_id={packet_id} len={len(data)} to {len(self.connections)} clients")

        for connection in self.connections.values():
            if exclude and connection.player == exclude:
                continue
            if gameplay and not connection.in_game:
                continue
            connection.send(data, reliable=reliable)
    
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
