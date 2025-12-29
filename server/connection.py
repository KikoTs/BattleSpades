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
    NewPlayerConnection, InitialInfo
)
from aoslib.bytes import ByteReader

if TYPE_CHECKING:
    import enet
    from server.main import BattleSpadesServer
    from server.player import Player

logger = logging.getLogger(__name__)


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
    
    def send(self, data: bytes, reliable: bool = True):
        """Send packet to this connection."""
        import enet
        
        flags = enet.PACKET_FLAG_RELIABLE if reliable else 0
        packet = enet.Packet(data, flags)
        self.peer.send(0, packet)
    
    def disconnect(self, reason: int = 0):
        """Disconnect this peer."""
        self.peer.disconnect(reason)
    
    async def send_map_data(self):
        """Send map data to client."""
        logger.debug(f"Sending map data to {self.peer.address}")
        
        # Send MapDataStart
        start_packet = MapDataStart()
        self.send(bytes(start_packet.generate()))
        
        # Get compressed map data
        chunker = self.server.world_manager.get_chunker()
        
        percent = 0
        for chunk in chunker.iter():
            chunk_packet = MapDataChunk()
            chunk_packet.percent_complete = min(99, percent)
            chunk_packet.data = chunk
            self.send(bytes(chunk_packet.generate()))
            percent += 1
        
        # Send MapDataEnd
        end_packet = MapDataEnd()
        self.send(bytes(end_packet.generate()))
        
        self.map_sent = True
        logger.debug(f"Map data sent to {self.peer.address}")
    
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
        reader = ByteReader(data[1:])
        
        # NewPlayerConnection (15)
        if packet_id == 15:
            packet = NewPlayerConnection(reader)
            await self._on_new_player(packet)
    
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
