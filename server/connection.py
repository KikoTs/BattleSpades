"""
Connection - ENet peer wrapper.
Handles map transfer and player state.
"""

import logging
import zlib
from typing import Optional, TYPE_CHECKING

from aoslib.packet import (
    MapDataStart, MapDataChunk, MapDataEnd,
    StateData, ExistingPlayer, CreatePlayer,
    NewPlayerConnection, InitialInfo, SteamSessionTicket
)
from aoslib.bytes import ByteReader

if TYPE_CHECKING:
    import enet
    from server.main import BattleSpadesServer
    from server.player import Player

logger = logging.getLogger(__name__)


def lzf_decompress(data: bytes) -> bytes:
    """Simple LZF decompression - fallback to zlib if LZF not available."""
    try:
        import lzf
        return lzf.decompress(data, len(data) * 10)
    except ImportError:
        # Fallback - try zlib
        try:
            return zlib.decompress(data)
        except:
            return data


class Connection:
    """
    Wraps an ENet peer connection.
    Manages handshake, map transfer, and player association.
    """
    
    def __init__(self, peer: 'enet.Peer', server: 'BattleSpadesServer'):
        self.peer = peer
        self.server = server
        self.player: Optional['Player'] = None
        
        # Connection state
        self.authenticated = False
        self.map_sent = False
        self.state_sent = False
        self.steam_key: Optional[bytes] = None
    
    def send(self, data: bytes, reliable: bool = True):
        """Send packet to this connection."""
        import enet
        
        # Add compression prefix (0x30 = uncompressed, 0x31 = LZF compressed)
        prefixed_data = bytes([0x30]) + data
        
        flags = enet.PACKET_FLAG_RELIABLE if reliable else 0
        packet = enet.Packet(prefixed_data, flags)
        self.peer.send(0, packet)
    
    def disconnect(self, reason: int = 0):
        """Disconnect this peer."""
        self.peer.disconnect(reason)
    
    def decrypt(self, data: bytes) -> bytes:
        """Decrypt packet data using steam key."""
        if not self.steam_key:
            return data
        return bytes(b ^ self.steam_key[i % len(self.steam_key)] for i, b in enumerate(data))
    
    def on_connect(self, data: int):
        """Called when connection is established."""
        logger.debug(f"Connection established from {self.peer.address} (protocol={data})")
        # Protocol version check could go here
        # For now just log it
    
    def on_disconnect(self):
        """Called when connection is closed."""
        logger.debug(f"Connection closed from {self.peer.address}")
    
    async def on_receive(self, data: bytes):
        """Handle incoming packet - dispatches to appropriate handler."""
        if len(data) < 2:
            return
        
        # Handle compression prefix
        if data[0] == 0x31:
            # LZF compressed
            data = lzf_decompress(data[1:])
        else:
            # Skip prefix byte
            data = data[1:]
        
        # Decrypt if we have steam key
        data = self.decrypt(data)
        
        if len(data) < 1:
            return
        
        packet_id = data[0]
        logger.debug(f"RECV packet_id={packet_id} len={len(data)} from {self.peer.address}")
        
        # Route to handler
        if self.player:
            # Forward to packet handler for joined players
            from protocol.packet_handler import PacketHandler
            handler = PacketHandler(self.server)
            await handler.handle(self.player, data)
        else:
            # Handle pre-join packets
            await self.handle_pre_join_packet(data)
    
    async def send_state_data(self, player_id: int):
        """Send game state to newly joined player."""
        state = StateData()
        
        state.player_id = player_id
        state.fog_color = self.server.config.fog_color
        state.gravity = -9.81  # Standard gravity
        
        # Light settings
        state.light_color = (255, 255, 255)
        state.light_direction = (0.5, 0.5, -0.5)
        state.back_light_color = (128, 128, 128)
        state.back_light_direction = (-0.5, -0.5, 0.5)
        state.ambient_light_color = (100, 100, 100)
        state.ambient_light_intensity = 0.5
        state.time_scale = 1.0
        
        # Game settings
        state.score_limit = self.server.config.score_limit
        state.mode_type = 0  # CTF
        state.team_headcount_type = 0
        
        # Team 1
        team1 = self.server.teams[0]
        state.team1_name = team1.name
        state.team1_color = team1.color
        state.team1_score = team1.score
        state.team1_classes = [0, 1, 2, 3]  # All classes available
        
        # Team 2
        team2 = self.server.teams[1]
        state.team2_name = team2.name
        state.team2_color = team2.color
        state.team2_score = team2.score
        state.team2_classes = [0, 1, 2, 3]
        
        state.prefabs = []
        state.entities = []
        state.screenshot_cameras_points = []
        state.screenshot_cameras_rotations = []
        state.has_map_ended = 0
        
        self.send(bytes(state.generate()))
        self.state_sent = True
    
    async def send_existing_players(self, new_player: 'Player'):
        """Send info about existing players to new player."""
        for player in self.server.players.values():
            if player.id == new_player.id:
                continue
            
            packet = ExistingPlayer()
            packet.player_id = player.id
            packet.demo_player = 0
            packet.team = player.team
            packet.class_id = player.class_id
            packet.tool = player.tool
            packet.pickup = 0
            packet.dead = 0 if player.alive else 1
            packet.score = player.kills
            packet.forced_team = 0
            packet.local_language = 0
            packet.color = player.color
            packet.name = player.name
            packet.loadout = []
            packet.prefabs = []
            
            self.send(bytes(packet.generate()))
    
    async def handle_pre_join_packet(self, data: bytes):
        """Handle packets before player is fully joined."""
        if len(data) < 1:
            return
        
        packet_id = data[0]
        logger.debug(f"PRE-JOIN packet_id={packet_id} len={len(data)} hex={data[:32].hex()}")
        
        reader = ByteReader(data[1:])
        
        # SteamSessionTicket (48) - client sends this first after connect
        if packet_id == 48:
            logger.info(f"Received SteamSessionTicket from {self.peer.address}")
            try:
                packet = SteamSessionTicket(reader)
                self.steam_key = getattr(packet, 'ticket', None)
                self.authenticated = True
                logger.debug(f"Steam authenticated, sending connection data")
                # Now send all connection data
                await self.send_connection_data()
            except Exception as e:
                logger.error(f"Error parsing SteamSessionTicket: {e}")
                # Still proceed even if parsing fails
                await self.send_connection_data()
        
        # NewPlayerConnection (15)
        elif packet_id == 15:
            logger.debug(f"Decoding NewPlayerConnection")
            packet = NewPlayerConnection(reader)
            await self._on_new_player(packet)
        else:
            logger.debug(f"Unknown pre-join packet ID: {packet_id}")
    
    async def send_connection_data(self):
        """Send all initial data to client after authentication."""
        logger.info(f"Sending connection data to {self.peer.address}")
        
        # Send initial info
        # await self.send_info()
        
        # Send map data
        await self.send_map_data()
        
        # Note: State and player list will be sent after client sends NewPlayerConnection
    
    async def send_map_data(self):
        """Send map data to client."""
        logger.debug(f"Sending map data to {self.peer.address}")
        
        # Send MapDataStart
        start_packet = MapDataStart()
        self.send(bytes(start_packet.generate()))
        
        # Get compressed map data
        chunker = self.server.world_manager.get_chunker()
        
        chunk_list = list(chunker.iter())
        total_chunks = len(chunk_list)
        
        for idx, chunk in enumerate(chunk_list):
            chunk_packet = MapDataChunk()
            chunk_packet.percent_complete = min(99, int((idx / max(1, total_chunks)) * 100))
            chunk_packet.data = chunk
            self.send(bytes(chunk_packet.generate()))
        
        # Send MapDataEnd
        end_packet = MapDataEnd()
        self.send(bytes(end_packet.generate()))
        
        self.map_sent = True
        logger.info(f"Map data sent to {self.peer.address} ({total_chunks} chunks)")
    
    async def _on_new_player(self, packet: NewPlayerConnection):
        """Handle new player joining."""
        from server.player import Player
        
        player_id = self.server.get_next_player_id()
        if player_id < 0:
            logger.warning("Server full, rejecting connection")
            self.disconnect(reason=3)  # Server full
            return
        
        # Create player
        player = Player(player_id, packet.name, self)
        player.team = packet.team
        player.class_id = packet.class_id
        
        self.player = player
        self.server.players[player_id] = player
        
        # Add to team
        if player.team in self.server.teams:
            self.server.teams[player.team].add_player(player)
        
        logger.info(f"Player {player.name} (ID {player_id}) joined team {player.team}")
        
        # Send state data
        await self.send_state_data(player_id)
        
        # Send existing players
        await self.send_existing_players(player)
        
        # Broadcast new player to others
        create_packet = CreatePlayer()
        create_packet.player_id = player_id
        create_packet.demo_player = 0
        create_packet.class_id = player.class_id
        create_packet.team = player.team
        create_packet.dead = 1
        create_packet.local_language = 0
        
        # Spawn position
        spawn = self.server.world_manager.get_spawn_point(player.team)
        create_packet.x = spawn[0]
        create_packet.y = spawn[1]
        create_packet.z = spawn[2]
        create_packet.ori_x = 1.0
        create_packet.ori_y = 0.0
        create_packet.ori_z = 0.0
        create_packet.name = player.name
        create_packet.loadout = []
        create_packet.prefabs = []
        
        self.server.broadcast(bytes(create_packet.generate()))
        
        # Spawn player
        player.spawn(spawn[0], spawn[1], spawn[2])
        
        # Notify game mode
        if self.server.mode:
            await self.server.mode.on_player_join(player)
