"""
BattleSpades Main Server
Ace of Spades Protocol 1.0 Battle Builders

Uses ENet for networking with asyncio integration.
"""

import asyncio
import logging
import time
from typing import Dict, Optional, TYPE_CHECKING

from aoslib.vxl import AceMap
from aoslib.packet import (
    WorldUpdate, StateData, CreatePlayer, ExistingPlayer, 
    PlayerLeft, MapDataStart, MapDataChunk, MapDataEnd,
    ClockSync, FogColor, ChatMessage
)
from aoslib.bytes import ByteWriter

from .config import ServerConfig
from .player import Player
from .team import Team
from .world_manager import WorldManager
from .connection import Connection

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
        
        # Teams
        self.teams = {
            0: Team(0, config.team1_name, config.team1_color),
            1: Team(1, config.team2_name, config.team2_color),
        }
        
        # World
        self.world_manager = WorldManager(config)
        
        # Game mode
        self.mode = None
    
    def get_player_by_name(self, name: str) -> Optional[Player]:
        """Find a player by name (case-insensitive partial match)."""
        name_lower = name.lower()
        for player in self.players.values():
            if player.name.lower().startswith(name_lower):
                return player
        return None
    
    def get_next_player_id(self) -> int:
        """Get the next available player ID."""
        for i in range(self.config.max_players):
            if i not in self.players:
                return i
        return -1
    
    async def start(self):
        """Initialize and start the server."""
        import enet
        
        logger.info(f"Starting BattleSpades server on port {self.config.port}")
        
        # Create ENet host
        address = enet.Address(b"0.0.0.0", self.config.port)
        self.host = enet.Host(
            address,
            peerCount=self.config.max_players,
            channelLimit=2,
            incomingBandwidth=0,
            outgoingBandwidth=0,
        )
        
        # Load map
        self.world_manager.load_map(self.config.map_name)
        
        # Initialize game mode
        from modes import get_mode_class
        mode_class = get_mode_class(self.config.game_mode)
        if mode_class:
            self.mode = mode_class(self)
            await self.mode.on_mode_start()
        
        self.running = True
        logger.info(f"Server started: {self.config.server_name}")
        
        # Run main loops
        await asyncio.gather(
            self._network_loop(),
            self._game_loop(),
            self._world_update_loop(),
        )
    
    async def stop(self):
        """Stop the server gracefully."""
        logger.info("Stopping server...")
        self.running = False
        
        if self.mode:
            await self.mode.on_mode_end()
        
        # Disconnect all players
        for connection in list(self.connections.values()):
            connection.disconnect()
        
        if self.host:
            self.host.flush()
            self.host = None
        
        logger.info("Server stopped")
    
    async def _network_loop(self):
        """Handle ENet events."""
        import enet
        
        while self.running:
            if self.host is None:
                break
            
            event = self.host.service(0)
            
            if event.type == enet.EVENT_TYPE_CONNECT:
                await self._on_connect(event.peer)
            
            elif event.type == enet.EVENT_TYPE_DISCONNECT:
                await self._on_disconnect(event.peer)
            
            elif event.type == enet.EVENT_TYPE_RECEIVE:
                await self._on_receive(event.peer, event.packet.data)
            
            await asyncio.sleep(0.001)  # 1ms poll interval
    
    async def _game_loop(self):
        """Main game tick loop at configured tick rate."""
        last_tick = time.perf_counter()
        
        while self.running:
            current = time.perf_counter()
            elapsed = current - last_tick
            
            if elapsed >= self.tick_interval:
                self.loop_count += 1
                last_tick = current
                
                # Update players
                for player in self.players.values():
                    player.update(self.tick_interval)
                
                # Update game mode
                if self.mode:
                    await self.mode.on_tick(self.loop_count)
            
            await asyncio.sleep(0.001)
    
    async def _world_update_loop(self):
        """Send world updates to clients at lower frequency."""
        update_interval = 1.0 / 20.0  # 20 Hz
        
        while self.running:
            # Build WorldUpdate packet
            world_update = WorldUpdate()
            world_update.loop_count = self.loop_count
            
            for pid, player in self.players.items():
                if player.alive:
                    world_update[pid] = (
                        (player.x, player.y, player.z),  # position
                        player.orientation,               # orientation
                        (player.vx, player.vy, player.vz),  # velocity
                        0,  # ping
                        0,  # pong
                        player.health,
                        player.get_input_byte(),
                        0,  # action
                        player.tool,
                    )
            
            # Broadcast update
            data = bytes(world_update.generate())
            self.broadcast(data)
            
            await asyncio.sleep(update_interval)
    
    async def _on_connect(self, peer):
        """Handle new connection."""
        peer_id = peer.incomingPeerID
        logger.info(f"New connection from {peer.address}")
        
        connection = Connection(peer, self)
        self.connections[peer_id] = connection
        
        # Send initial handshake / map data
        await connection.send_map_data()
    
    async def _on_disconnect(self, peer):
        """Handle disconnection."""
        peer_id = peer.incomingPeerID
        
        connection = self.connections.pop(peer_id, None)
        if connection and connection.player:
            player = connection.player
            logger.info(f"Player {player.name} disconnected")
            
            # Remove from team
            if player.team in self.teams:
                self.teams[player.team].remove_player(player)
            
            # Remove from players
            self.players.pop(player.id, None)
            
            # Notify game mode
            if self.mode:
                await self.mode.on_player_leave(player)
            
            # Broadcast disconnect
            left_packet = PlayerLeft()
            left_packet.player_id = player.id
            self.broadcast(bytes(left_packet.generate()))
    
    async def _on_receive(self, peer, data: bytes):
        """Handle incoming packet."""
        peer_id = peer.incomingPeerID
        connection = self.connections.get(peer_id)
        
        if not connection:
            return
        
        # Route to packet handler
        from protocol.packet_handler import PacketHandler
        handler = PacketHandler(self)
        
        if connection.player:
            await handler.handle(connection.player, data)
        else:
            # Handle pre-join packets (NewPlayerConnection, etc.)
            await connection.handle_pre_join_packet(data)
    
    def broadcast(self, data: bytes, exclude: Optional[Player] = None):
        """Send packet to all connected players."""
        import enet
        
        for connection in self.connections.values():
            if exclude and connection.player == exclude:
                continue
            connection.send(data)
    
    def broadcast_team(self, team_id: int, data: bytes):
        """Send packet to all players on a team."""
        for connection in self.connections.values():
            if connection.player and connection.player.team == team_id:
                connection.send(data)
