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
from .a2s_query import A2SHandler

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
        
        # A2S Query handler for Steam browser and LAN discovery
        self.a2s_handler = A2SHandler(self)
        
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
        
        self.running = True
        logger.info(f"Server started: {self.config.server_name}")
        
        # Run main loops
        await asyncio.gather(
            self._network_loop(),
            self._game_loop(),
            self._world_update_loop(),
        )
    
    async def stop(self):
        """Stop the server."""
        if not self.running:
            return
        
        logger.info("Stopping server...")
        self.running = False
        
        if self.mode:
            await self.mode.on_mode_end()
        
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
                    logger.debug(f"ENET RECEIVE from {peer.address} len={len(event.packet.data)}")
                    asyncio.create_task(self._on_receive(peer, event.packet))
                    
            except Exception as e:
                logger.error(f"Error in net_update: {e}", exc_info=True)
    
    async def _network_loop(self):
        """Handle ENet events."""
        while self.running:
            if self.host is None:
                break
            
            self._net_update()
            await asyncio.sleep(1/60)  # 60 Hz network update
    
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
                
                # Update A2S handler
                self.a2s_handler.update()
                
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
    
    def _on_connect_sync(self, peer, data: int = 0):
        """Handle new connection (sync version for net_update)."""
        logger.info(f"New connection from {peer.address} (proto_ver={data})")
        
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
    
    async def _on_receive(self, peer, packet):
        """Handle received packet."""
        connection = self.connections.get(peer)
        if not connection:
            return
        
        # Get packet data
        data = bytes(packet.data)
        
        # Let connection handle packet routing (includes decompression, decryption)
        await connection.on_receive(data)
    
    def get_connection(self, peer):
        """Get connection for a peer."""
        return self.connections.get(peer)
    
    def broadcast(self, data: bytes, exclude: Optional[Player] = None):
        """Send packet to all connected players."""
        import enet
        
        packet_id = data[0] if len(data) > 0 else -1
        if packet_id not in self.config.log_suppress_packets:
            logger.debug(f"SEND broadcast packet_id={packet_id} len={len(data)} to {len(self.connections)} clients")
        
        for connection in self.connections.values():
            if exclude and connection.player == exclude:
                continue
            connection.send(data)
    
    def broadcast_team(self, team_id: int, data: bytes):
        """Send packet to all players on a team."""
        packet_id = data[0] if len(data) > 0 else -1
        if packet_id not in self.config.log_suppress_packets:
            logger.debug(f"SEND team={team_id} packet_id={packet_id} len={len(data)}")
        
        for connection in self.connections.values():
            if connection.player and connection.player.team == team_id:
                connection.send(data)
