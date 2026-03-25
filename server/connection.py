"""
Connection - ENet peer wrapper.
Handles map transfer and player state.
"""

import asyncio
import inspect
import logging
import zlib
from typing import Optional, TYPE_CHECKING, Dict, Type

import shared.packet as shared_packet
from protocol.runtime_packets import decode_runtime_packet
from shared.bytes import ByteReader
from shared.packet import (
    ClientInMenu,
    ClockSync,
    CreatePlayer,
    ExistingPlayer,
    InitialInfo,
    MapDataValidation,
    MapSyncChunk,
    MapSyncEnd,
    MapSyncStart,
    NewPlayerConnection,
    SetClassLoadout,
    SetHP,
    SkyboxData,
    StateData,
    SteamSessionTicket,
)
from server.game_constants import (
    DEFAULT_TEAM_CLASSES,
    DEFAULT_WEAPON_TOOL,
    TEAM1,
    TEAM2,
    TEAM_NEUTRAL,
    TEAM_SPECTATOR,
    is_playable_team,
)

# Build packet name mapping
PACKET_NAMES = {}
for name, cls in inspect.getmembers(shared_packet):
    if inspect.isclass(cls) and hasattr(cls, 'id') and isinstance(cls.id, int):
        PACKET_NAMES[cls.id] = name

def get_packet_name(packet_id: int) -> str:
    return PACKET_NAMES.get(packet_id, "Unknown")

def format_packet_fields(packet) -> str:
    """Format packet fields for debug logging."""
    fields = []
    # Get all public attributes (exclude private/dunder and methods)
    for attr in dir(packet):
        if attr.startswith('_') or attr in ('id', 'read', 'write', 'generate', 'compress_packet'):
            continue
        try:
            value = getattr(packet, attr)
            # Skip methods and callables
            if callable(value):
                continue
            # Truncate long lists/bytes for readability
            if isinstance(value, (list, tuple)) and len(value) > 5:
                value = f"{type(value).__name__}[{len(value)} items]"
            elif isinstance(value, bytes) and len(value) > 32:
                value = f"bytes[{len(value)}]"
            elif isinstance(value, str) and len(value) > 50:
                value = f"'{value[:50]}...'"
            fields.append(f"{attr}={value!r}")
        except Exception:
            pass
    return ", ".join(fields) if fields else "(no fields)"

def try_parse_packet_for_logging(packet_id: int, data: bytes):
    """Try to parse packet data and return formatted fields, or None if failed."""
    if packet_id not in PACKET_NAMES:
        return None
    packet_name = PACKET_NAMES[packet_id]
    try:
        runtime_packet = decode_runtime_packet(packet_id, data[1:])
        if runtime_packet is not None:
            return format_packet_fields(runtime_packet)
        packet_class = getattr(shared_packet, packet_name, None)
        if packet_class is None:
            return None
        reader = ByteReader(data[1:])  # Skip packet ID byte
        packet = packet_class()
        packet.read(reader)
        return format_packet_fields(packet)
    except Exception as e:
        return f"(parse error: {e})"

if TYPE_CHECKING:
    import enet
    from server.main import BattleSpadesServer
    from server.player import Player

logger = logging.getLogger(__name__)


from server.util import lzf_compress, lzf_decompress


WIRE_TEAM_SPECTATOR = TEAM_SPECTATOR
WIRE_TEAM_NEUTRAL = TEAM_NEUTRAL
DEFAULT_WIRE_TEAM = TEAM1
SPAWN_HP_DAMAGE_TYPE = 2


def wire_team_to_internal(team_id: int) -> Optional[int]:
    """Convert wire team IDs into reversed runtime team IDs."""
    return team_id if is_playable_team(team_id) else None


def internal_team_to_wire(team_id: int) -> int:
    """Convert runtime team IDs into the wire representation."""
    return team_id if is_playable_team(team_id) else DEFAULT_WIRE_TEAM


def is_non_playable_wire_team(team_id: int) -> bool:
    """Return True for spectator/neutral wire team IDs."""
    return team_id in (WIRE_TEAM_SPECTATOR, WIRE_TEAM_NEUTRAL)


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
        self.steam_key: Optional[bytes] = None  # Set when SteamSessionTicket received
        self.pending_class_id: Optional[int] = None
        self.pending_loadout: list[int] = []
        self.pending_prefabs: list[str] = []
        self.pending_ugc_tools: list[int] = []
        self.in_menu: Optional[bool] = None
        
        # Packet waiting
        self._waiters: Dict[int, asyncio.Future] = {}
    
    def send(self, data: bytes, reliable: bool = True, prefix: int = 0x30):
        """Send packet to this connection."""
        import enet
        
        # Log raw packet (unless suppressed)
        packet_id = data[0] if len(data) > 0 else -1
        suppressed = packet_id in self.server.config.log_suppress_packets
        
        if not suppressed:
            hex_data = ' '.join(f'{b:02X}' for b in data)
            packet_name = get_packet_name(packet_id)
            parsed_fields = try_parse_packet_for_logging(packet_id, data)
            logger.debug(f"SEND packet_id={packet_id} ({packet_name}) len={len(data)} hex={hex_data} to {self.peer.address}")
            if parsed_fields:
                logger.debug(f"  -> Fields: {parsed_fields}")
        
        # Add compression (chunking using fake LZF) and prefix
        compressed = lzf_compress(data)
        prefixed_data = bytes([prefix]) + compressed
        
        # Log compressed packet
        if not suppressed:
             comp_hex = ' '.join(f'{b:02X}' for b in prefixed_data)
             logger.debug(f"Compressed Hex Data: {comp_hex}")
        
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

    def send_clock_sync_response(self, client_time: int):
        """Reply to a client clock sync packet."""
        packet = ClockSync()
        packet.client_time = client_time
        packet.server_loop_count = self.server.loop_count
        self.send(bytes(packet.generate()))

    def _cache_pre_join_loadout(self, packet: SetClassLoadout):
        """Store pre-join loadout state for the first spawn."""
        self.pending_class_id = packet.class_id
        self.pending_loadout = list(packet.loadout)
        self.pending_prefabs = list(packet.prefabs)
        self.pending_ugc_tools = list(packet.ugc_tools)

    def _resolve_join_team(self, wire_team: int) -> tuple[int, int]:
        """Resolve the initial playable team from the client's wire team ID."""
        internal_team = wire_team_to_internal(wire_team)
        if internal_team is not None:
            return internal_team, wire_team

        fallback_internal = wire_team_to_internal(DEFAULT_WIRE_TEAM)
        if is_non_playable_wire_team(wire_team):
            logger.warning(
                "Client requested non-playable wire team %s during initial spawn; defaulting to %s",
                wire_team,
                DEFAULT_WIRE_TEAM,
            )
        else:
            logger.warning(
                "Unknown wire team %s during initial spawn; defaulting to %s",
                wire_team,
                DEFAULT_WIRE_TEAM,
            )
        return fallback_internal, internal_team_to_wire(fallback_internal)

    def _send_spawn_hp(self, hp: int = 100):
        """Send the initial HP packet expected immediately after first spawn."""
        packet = SetHP()
        packet.hp = hp
        packet.damage_type = SPAWN_HP_DAMAGE_TYPE
        packet.source_x = 0.0
        packet.source_y = 0.0
        packet.source_z = 0.0
        self.send(bytes(packet.generate()))
    
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
        
        # Once the Steam ticket has been received, subsequent packets are XOR-encrypted.
        data = self.decrypt(data)
        
        if len(data) < 1:
            return
        
        packet_id = data[0]
        #packet_name = get_packet_name(packet_id) # Using this here might spam console if on_receive is called a lot. Use sparingly? No user asked for it.
        # Check suppression for receive too? Assuming yes.
        suppressed = packet_id in self.server.config.log_suppress_packets
        if not suppressed:
             packet_name = get_packet_name(packet_id)
             hex_data = ' '.join(f'{b:02X}' for b in data)
             parsed_fields = try_parse_packet_for_logging(packet_id, data)
             logger.debug(f"RECV packet_id={packet_id} ({packet_name}) len={len(data)} hex={hex_data} from {self.peer.address}")
             if parsed_fields:
                 logger.debug(f"  -> Fields: {parsed_fields}")
        
        # Check if anyone is waiting for this packet
        if packet_id in self._waiters:
            future = self._waiters[packet_id]
            if not future.done():
                future.set_result(data)
                return # Consume the packet
        
        # Route to handler
        if self.player:
            # Forward to packet handler for joined players
            from protocol.packet_handler import PacketHandler
            handler = PacketHandler(self.server)
            await handler.handle(self.player, data)
        else:
            # Handle pre-join packets
            # Handle pre-join packets
            await self.handle_pre_join_packet(data)

    async def wait_for(self, packet_class: Type, timeout: float = 5.0):
        """Wait for a specific packet type."""
        packet_id = packet_class.id
        future = asyncio.Future()
        self._waiters[packet_id] = future
        
        try:
            data = await asyncio.wait_for(future, timeout)
            # Parse packet
            reader = ByteReader(data[1:]) # Skip ID
            packet = packet_class()
            packet.read(reader)
            return packet
        finally:
            if packet_id in self._waiters:
                del self._waiters[packet_id]
    
    async def send_state_data(self, player_id: int = -1):
        """Send game state to newly joined player."""
        state = StateData()
        
        state.player_id = player_id if player_id != -1 else 0 # Placeholder or 255 for "not assigned yet"?
        state.fog_color = self.server.config.fog_color
        state.gravity = 1.0
        
        # Light settings
        state.light_color = (180, 192, 220)
        state.light_direction = (0.203125, 0.796875, 0.0)
        state.back_light_color = (64, 64, 64)
        state.back_light_direction = (-0.078125, -0.578125, 0.296875)
        state.ambient_light_color = (52, 56, 64)
        state.ambient_light_intensity = 0.203125
        state.time_scale = 1.0
        
        # Game settings
        state.score_limit = self.server.config.score_limit
        state.mode_type = 8  # CTF
        state.team_headcount_type = 6
        
        # Team 1
        team1 = self.server.teams[TEAM1]
        state.team1_name = team1.name
        state.team1_color = team1.color
        state.team1_score = team1.score
        state.team1_classes = DEFAULT_TEAM_CLASSES  # All classes available
        
        # Team 2
        team2 = self.server.teams[TEAM2]
        state.team2_name = team2.name
        state.team2_color = team2.color
        state.team2_score = team2.score
        state.team2_classes = DEFAULT_TEAM_CLASSES
        
        state.prefabs = ['supertower']
        state.entities = []
        state.screenshot_cameras_points = [(0.0, 0.0, 0.0)]
        state.screenshot_cameras_rotations = [(0.0, 0.0, 0.0)]
        state.has_map_ended = 0
        
        self.send(bytes(state.generate()), prefix=0x31)
        self.state_sent = True

    async def send_skybox(self):
        """Send skybox data to client."""
        skybox = SkyboxData()
        skybox.value = "Chicago.txt"
        self.send(bytes(skybox.generate()), prefix=0x30)
    
    async def send_existing_players(self, new_player: 'Player' = None):
        """Send info about existing players to new player."""
        for player in self.server.players.values():
            if new_player and player.id == new_player.id:
                continue
            
            packet = ExistingPlayer()
            packet.player_id = player.id
            packet.demo_player = 0
            packet.team = internal_team_to_wire(player.team)
            packet.class_id = player.class_id
            packet.tool = player.tool
            packet.pickup = 0
            packet.dead = 0 if player.alive else 1
            packet.score = player.kills
            packet.forced_team = 0
            packet.local_language = 0
            packet.color = getattr(player, 'color', player.block_color)
            packet.name = player.name
            packet.loadout = []
            packet.prefabs = []
            
            self.send(bytes(packet.generate()))
    
    async def handle_pre_join_packet(self, data: bytes):
        """Handle packets before player is fully joined."""
        if len(data) < 1:
            return
        
        packet_id = data[0]
        packet_name = get_packet_name(packet_id)
        logger.debug(f"PRE-JOIN packet_id={packet_id} ({packet_name}) len={len(data)} hex={data[:32].hex()}")
        
        reader = ByteReader(data[1:])
        
        # SteamSessionTicket (105) - client sends this first after connect
        if packet_id == 105:
            logger.info(f"Received SteamSessionTicket from {self.peer.address}")
            try:
                packet = SteamSessionTicket(reader)
                # Set steam key immediately - subsequent packets will be decrypted
                self.steam_key = getattr(packet, 'ticket', None)
                self.authenticated = True
                if self.steam_key:
                    logger.debug(f"Steam key set, len={len(self.steam_key)}")
                else:
                    logger.debug(f"No steam key (offline mode)")
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
        elif packet_id == SetClassLoadout.id:
            packet = SetClassLoadout(reader)
            self._cache_pre_join_loadout(packet)
            logger.debug(
                "Cached pre-join loadout: class_id=%s loadout=%s prefabs=%s ugc_tools=%s",
                packet.class_id,
                packet.loadout,
                packet.prefabs,
                packet.ugc_tools,
            )
        elif packet_id == ClockSync.id:
            packet = ClockSync(reader)
            self.send_clock_sync_response(packet.client_time)
        elif packet_id == ClientInMenu.id:
            packet = ClientInMenu(reader)
            self.in_menu = bool(packet.in_menu)
            logger.debug("Client pre-join menu state updated: in_menu=%s", self.in_menu)
        else:
            logger.debug(f"Unknown pre-join packet ID: {packet_id}")
    
    async def send_connection_data(self):
        """Send all initial data to client after authentication."""
        logger.info(f"Sending connection data to {self.peer.address}")
        
        # Send initial info
        await self.send_info()
        
        # Send map data
        await self.send_map_data()

        # Notes: State and players should sent "right away" (before NewPlayerConnection)
        await self.send_state_data()
        await self.send_skybox()
        await self.send_existing_players()


    async def send_info(self):
        """Send InitialInfo (packet 114) to client."""
        packet = InitialInfo()
        
        # Server info
        packet.server_steam_id = 90087911866072064
        packet.server_ip = 0
        packet.server_port = self.server.config.port
        packet.query_port = self.server.config.port
        packet.server_name = self.server.config.server_name
        
        # Game mode info // hardcoded ctf
        packet.mode_name = "CTF_TITLE"
        packet.mode_description = "CTF_DESCRIPTION"
        packet.mode_infographic_text1 = "CTF_INFOGRAPHIC_TEXT1"
        packet.mode_infographic_text2 = "CTF_INFOGRAPHIC_TEXT2"
        packet.mode_infographic_text3 = "CTF_INFOGRAPHIC_TEXT3"
        packet.mode_key = 8
        
        # Map info
        packet.map_name = self.server.world_manager.map_name if self.server.world_manager else self.server.config.map_name
        packet.filename = "London" # Hard coded, why the fuck check the client map when the server sends it over the air any fucking way?
        packet.checksum = 592649088 # For same reason as above
        packet.map_is_ugc = 0
        packet.ugc_mode = 8
        
        # Game rules & Settings
        packet.classic = 0
        packet.enable_minimap = 1
        packet.same_team_collision = 0
        packet.max_draw_distance = 192
        packet.enable_colour_picker = 1
        packet.enable_colour_palette = 0
        packet.enable_deathcam = 1
        packet.enable_sniper_beam = 1
        packet.enable_spectator = 1
        packet.exposed_teams_always_on_minimap = 0
        packet.enable_numeric_hp = 1
        packet.texture_skin = None
        packet.beach_z_modifiable = 1
        packet.enable_minimap_height_icons = 0
        packet.enable_fall_on_water_damage = 1
        packet.block_wallet_multiplier = 1.0
        packet.block_health_multiplier = 1.0
        packet.enable_player_score = 1
        packet.allow_shooting_holding_intel = 1
        packet.friendly_fire = 1
        packet.enable_corpse_explosion = 1
        
        # Initialize lists
        packet.disabled_tools = [0]
        packet.disabled_classes = []
        packet.movement_speed_multipliers = [1.40625, 1.59375, 1.09375, 1.25, 1.40625, 1.65625, 1.328125, 1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 3.0, 3.0, 1.0, 1.546875, 1.34375]
        packet.ugc_prefab_sets = [0, 1]
        packet.ground_colors = [(59, 58, 55, 238), (40, 54, 64, 239)]
        packet.custom_game_rules = []
        packet.loadout_overrides = {}
        
        # Send
        self.send(bytes(packet.generate()))
        logger.info(f"Sent InitialInfo to {self.peer.address}")
    
    async def send_map_data(self):
        """Send map data to client."""
        logger.debug(f"Sending map data to {self.peer.address}")
        client_crc = None
        
        # Wait for MapDataValidation from client
        try:
            client_validation = await self.wait_for(MapDataValidation, timeout=5.0)
            client_crc = client_validation.crc
            logger.info(f"Client sent map CRC: {client_crc}")

            # send map data validation
            packet = MapDataValidation()
            packet.crc = client_crc
            self.send(bytes(packet.generate()), prefix=0x31)
            
            # Allow client to process validation and state change
            await asyncio.sleep(0.1)
            
        except asyncio.TimeoutError:
            logger.warning(f"Client {self.peer.address} timed out sending MapDataValidation")
        
        # Send MapSyncStart
        start_packet = MapSyncStart()
        
        # Get compressed map data
        chunker = self.server.world_manager.get_chunker()
        if chunker is None:
            logger.warning("No map chunker available for %s", self.peer.address)
            return
        
        self.send(bytes(start_packet.generate()), prefix=0x32)
        
        chunk_list = list(chunker.iter())
        total_chunks = len(chunk_list)
        server_crc = int(getattr(chunker, "crc32", 0))
        estimated_size = int(getattr(getattr(self.server.world_manager, "map", None), "estimated_size", 0))
        logger.info(
            "Prepared map sync for %s: chunks=%s server_crc=%s estimated_size=%s",
            self.peer.address,
            total_chunks,
            server_crc,
            estimated_size,
        )
        if client_crc is not None and client_crc != server_crc:
            logger.warning(
                "Client/server map CRC mismatch for %s: client=%s server=%s",
                self.peer.address,
                client_crc,
                server_crc,
            )
        
        for idx, chunk in enumerate(chunk_list):
            chunk_packet = MapSyncChunk()
            # User fix: int((index / total_chunks) * 100)
            chunk_packet.percent_complete = int((idx / max(total_chunks, 1)) * 100) + 1
            
            chunk_packet.data = chunk
            self.send(bytes(chunk_packet.generate()), prefix=0x31)
        
        # Send MapSyncEnd
        end_packet = MapSyncEnd()
        self.send(bytes(end_packet.generate()), prefix=0x31)

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
        
        # NewPlayerConnection does not carry a concrete tool, so start with the default weapon tool.
        weapon = DEFAULT_WEAPON_TOOL
        internal_team, wire_team = self._resolve_join_team(packet.team)
        player = Player(player_id, packet.name, internal_team, weapon, self)
        player.class_id = (
            self.pending_class_id if self.pending_class_id is not None else packet.class_id
        )
        
        self.player = player
        self.server.players[player_id] = player
        
        # Add to team
        if player.team in self.server.teams:
            self.server.teams[player.team].add_player(player)
        
        logger.info(
            "Player %s (ID %s) joined wire team %s as internal team %s",
            player.name,
            player_id,
            wire_team,
            player.team,
        )
        
        # State, Skybox and Existing players are sent in send_connection_data
        
        # Broadcast new player to others
        create_packet = CreatePlayer()
        create_packet.player_id = player_id
        create_packet.demo_player = 0
        create_packet.class_id = player.class_id
        create_packet.team = wire_team
        create_packet.dead = 0
        create_packet.local_language = packet.local_language
        
        # Spawn position
        spawn = self.server.world_manager.get_spawn_point(player.team)
        create_packet.x = spawn[0]
        create_packet.y = spawn[1]
        create_packet.z = spawn[2]
        create_packet.ori_x = 0.0
        create_packet.ori_y = 0.0
        create_packet.ori_z = 255.5
        create_packet.name = player.name
        create_packet.loadout = list(self.pending_loadout)
        create_packet.prefabs = list(self.pending_prefabs)
        
        self.server.broadcast(bytes(create_packet.generate()))
        
        # Spawn player
        player.spawn(spawn[0], spawn[1], spawn[2])
        self._send_spawn_hp()
        
        # Notify game mode
        if self.server.mode:
            await self.server.mode.on_player_join(player)
