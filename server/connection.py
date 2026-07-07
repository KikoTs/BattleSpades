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
        # The client learns ITS OWN id from StateData.player_id, which is
        # sent BEFORE the Player object exists — so the id must be reserved
        # at connection-data time and reused at NewPlayerConnection. Sending
        # the default 0 breaks identity whenever id 0 is taken (e.g. bots):
        # the client mistakes another player for itself (ghost self at
        # spawn, corrections toward the id-0 player, icon KeyErrors).
        self.reserved_player_id: Optional[int] = None

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
        # True once the client is fully in the GameScene (it has started its
        # input loop = first ClientData received). Until then the server must
        # NOT send it gameplay broadcasts (other players' CreatePlayer/Kill/
        # ChatMessage/StateData/WorldUpdate) — a flood during the async world
        # build / GameScene transition crashes the compiled client natively.
        self.in_game = False

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
        """Reply to a client clock sync packet.

        clock_sync_loop_bias (+1 default) makes the client pace its clock
        one tick AHEAD of ours, so its ClientData for frame N arrives
        before our tick N simulates — deterministic input timing instead
        of a per-packet race (see ServerConfig.clock_sync_loop_bias).
        """
        packet = ClockSync()
        packet.client_time = client_time
        packet.server_loop_count = (
            self.server.loop_count
            + int(getattr(self.server.config, "clock_sync_loop_bias", 0))
        )
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
        # Restock(69) refills the client's tool counters (grenades included).
        # Without it a fresh spawn may have zero grenades to throw.
        from shared.packet import Restock
        restock = Restock()
        restock.player_id = self.player.id if self.player else 0
        restock.type = 0
        self.send(bytes(restock.generate()))
    
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
        
        # First ClientData (4) means the client has entered the GameScene and
        # started its input loop. NOW it can safely receive the live roster +
        # entities and the ongoing gameplay broadcast stream — never before.
        if packet_id == 4 and self.player is not None and not self.in_game:
            self.in_game = True
            try:
                self.server.reveal_world_to(self)
            except Exception:
                logger.debug("reveal_world_to failed", exc_info=True)

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
        """Send game state to newly joined player.

        The actual packet construction lives in server.builders.state_data
        so any per-mode/per-config field changes happen in one place.
        """
        from server.builders import build_state_data
        state = build_state_data(self.server, player_id=player_id)
        self.send(bytes(state.generate()), prefix=0x31)
        self.state_sent = True

    async def send_skybox(self):
        """Send skybox data to client."""
        skybox = SkyboxData()
        skybox.value = "Chicago.txt"
        self.send(bytes(skybox.generate()), prefix=0x30)
    
    async def send_existing_players(self, new_player: 'Player' = None):
        """Announce the current roster to a joining client.

        Uses CreatePlayer (not ExistingPlayer) on purpose. MEASURED on the
        UNMODIFIED Steam client (2026-07-06): ExistingPlayer stores its
        `pickup` byte VERBATIM as the player's pickup_id — there is NO
        "no pickup" sentinel (0, 255 and everything else are stored as-is)
        — and the compiled minimap does `PICKUPS[pickup_id]` for any
        non-None pickup_id, where PICKUPS only has keys {14, 15, 16}
        (BOMB/DIAMOND/INTEL). Every value we can send either crashes the
        stock client with KeyError (the long-standing "minimap KeyError 0"
        crash) or paints a false pickup icon. CreatePlayer processing
        leaves pickup_id = None (the correct normal-icon state), carries
        the same roster data PLUS a live position, and is already
        broadcast for every respawn without issues. Players genuinely
        carrying a pickup announce it via the pickup packets.
        """
        for player in self.server.players.values():
            if new_player and player.id == new_player.id:
                continue
            if not player.alive or not player.spawned:
                continue

            packet = CreatePlayer()
            packet.player_id = player.id
            packet.demo_player = 0
            packet.class_id = player.class_id
            packet.team = internal_team_to_wire(player.team)
            packet.dead = 0
            packet.local_language = getattr(player, 'local_language', 0)
            packet.x, packet.y, packet.z = player.x, player.y, player.z
            # Real orientation unit vector — a degenerate one NaNs the
            # client's look-at basis (see _broadcast_create_player).
            packet.ori_x = player.o_x
            packet.ori_y = player.o_y
            packet.ori_z = player.o_z
            packet.name = player.name
            packet.loadout = list(getattr(player, 'loadout', []) or [])
            packet.prefabs = list(getattr(player, 'prefabs', []) or [])

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

        # Reserve this client's player id NOW: StateData must carry the same
        # id the Player will get at NewPlayerConnection (see __init__ note).
        if self.reserved_player_id is None:
            reserved = self.server.get_next_player_id()
            if reserved >= 0:
                self.reserved_player_id = reserved
                self.server.reserved_player_ids.add(reserved)

        # Notes: State and players should sent "right away" (before NewPlayerConnection)
        await self.send_state_data(
            self.reserved_player_id if self.reserved_player_id is not None else -1
        )
        await self.send_skybox()
        await self.send_existing_players()


    async def send_info(self):
        """Send InitialInfo (packet 114) to client.

        All field construction is in server.builders.initial_info — one
        place to drive map/mode/multipliers from real state.
        """
        from server.builders import build_initial_info
        packet = build_initial_info(self.server)
        self.send(bytes(packet.generate()))
        logger.info(
            "Sent InitialInfo to %s map=%s mode_key=%d crc=%d",
            self.peer.address, packet.map_name, packet.mode_key, packet.checksum,
        )


    async def send_map_data(self):
        """Send map data to client.

        Validation contract (measured against the original client + maps):
        - InitialInfo.checksum and our MapDataValidation reply both carry
          the CRC32 of the RAW .vxl file bytes; the client compares them
          against the crc32 of its local copy of `filename`.
        - On a match the client loads its pristine local file as the world
          base, so the MapSync stream only needs the columns changed since
          map load (empty on a fresh map).
        - On a mismatch we stream the full world state as sync chunks (the
          client applies (x, y, column) records onto whatever base it has).
        """
        logger.debug(f"Sending map data to {self.peer.address}")
        wm = self.server.world_manager
        server_crc = int(getattr(wm, "map_file_crc", 0)) & 0xFFFFFFFF
        server_crc_wire = server_crc - (1 << 32) if server_crc >= (1 << 31) else server_crc
        client_crc = None

        # Wait for MapDataValidation from client (its local file CRC)
        try:
            client_validation = await self.wait_for(MapDataValidation, timeout=5.0)
            client_crc = client_validation.crc
            logger.info(f"Client sent map CRC: {client_crc}")
        except asyncio.TimeoutError:
            logger.warning(f"Client {self.peer.address} timed out sending MapDataValidation")

        # Reply with OUR map file CRC — the truth, never an echo of the
        # client's value (echoing made every client believe its local map
        # was the right base regardless of which map we actually run).
        packet = MapDataValidation()
        packet.crc = server_crc_wire
        self.send(bytes(packet.generate()), prefix=0x31)

        # Allow client to process validation and state change
        await asyncio.sleep(0.1)

        crc_match = (
            client_crc is not None
            and (int(client_crc) & 0xFFFFFFFF) == server_crc
        )
        sync_mode = str(getattr(self.server.config, "map_sync_mode", "auto")).lower()
        use_delta = crc_match and sync_mode != "full"

        if use_delta:
            payload = wm.serialize_dirty_columns_compressed()
            chunk_list = [
                payload[i:i + 1024] for i in range(0, len(payload), 1024)
            ]
            logger.info(
                "Map CRC match for %s (crc=%s): delta sync, %s dirty columns, %s bytes",
                self.peer.address,
                server_crc,
                len(getattr(wm, "dirty_columns", ()) or ()),
                len(payload),
            )
        else:
            chunker = wm.get_chunker()
            if chunker is None:
                logger.warning("No map chunker available for %s", self.peer.address)
                return
            chunk_list = list(chunker.iter())
            if client_crc is not None and not crc_match:
                logger.warning(
                    "Client/server map file CRC mismatch for %s: client=%s server=%s "
                    "— streaming full world state",
                    self.peer.address,
                    client_crc,
                    server_crc,
                )

        total_chunks = len(chunk_list)
        total_size = sum(len(chunk) for chunk in chunk_list)
        logger.info(
            "Prepared map sync for %s: chunks=%s size=%s server_file_crc=%s mode=%s",
            self.peer.address,
            total_chunks,
            total_size,
            server_crc,
            "delta" if use_delta else "full",
        )

        start_packet = MapSyncStart()
        start_packet.size = total_size
        self.send(bytes(start_packet.generate()), prefix=0x32)

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

        # Use the id already promised to this client in StateData.player_id
        # (reserved during send_connection_data) — the client's whole
        # identity model is keyed on it.
        if self.reserved_player_id is not None and self.reserved_player_id >= 0:
            player_id = self.reserved_player_id
            self.server.reserved_player_ids.discard(player_id)
        else:
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
        # Real orientation unit vector (default (1,0,0)). NEVER send a
        # degenerate vector like (0,0,255.5): the client builds a non-local
        # player's look-at basis from this, and a forward vector parallel to
        # the z-up axis (or non-unit) NaNs the matrix and natively crashes the
        # renderer when it instantiates the model.
        create_packet.ori_x = player.o_x
        create_packet.ori_y = player.o_y
        create_packet.ori_z = player.o_z
        create_packet.name = player.name
        create_packet.loadout = list(self.pending_loadout)
        create_packet.prefabs = list(self.pending_prefabs)
        # Persist the choices on the player: loadout drives jetpack/ammo at
        # spawn; prefabs feed the prefab allow-list (BuildPrefabAction).
        player.loadout = list(self.pending_loadout)
        player.prefabs = list(self.pending_prefabs)

        # The joiner needs its OWN CreatePlayer to bind its local player to the
        # server id — deliver it directly (one packet is safe mid-transition).
        # Other players only learn about the joiner once THEY'RE in-game, so
        # broadcast (gameplay-gated) reaches just settled clients.
        create_bytes = bytes(create_packet.generate())
        self.send(create_bytes)
        self.server.broadcast(create_bytes, exclude=player)
        
        # Spawn player
        player.spawn(spawn[0], spawn[1], spawn[2])
        self._send_spawn_hp()
        if getattr(self.server, 'debug_parity', None) is not None:
            self.server.debug_parity.on_player_join(player)
        
        # Notify game mode
        if self.server.mode:
            await self.server.mode.on_player_join(player)

        # NB: the live roster + map entities are revealed to this client on its
        # FIRST ClientData (server.reveal_world_to), i.e. once it's actually in
        # the GameScene — never in the join handshake (that floods the
        # transition and crashes the client). See on_receive / reveal_world_to.
