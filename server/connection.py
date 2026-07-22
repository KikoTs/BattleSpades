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
from shared.constants import ERROR_DATA
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
    SetColor,
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
from server.map_metadata import DEFAULT_SKYBOX_NAME, normalize_skybox_name

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
    if is_playable_team(team_id) or team_id == WIRE_TEAM_SPECTATOR:
        return team_id
    return None


def internal_team_to_wire(team_id: int) -> int:
    """Convert runtime team IDs into the wire representation."""
    if is_playable_team(team_id) or team_id == TEAM_SPECTATOR:
        return team_id
    return DEFAULT_WIRE_TEAM


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
        self.pending_selection = None
        # Compatibility mirrors for callers still constructing the old
        # handshake fields independently. Runtime join logic consumes the
        # atomic selection above (or normalizes these mirrors once).
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
        # Terrain sequence serialized into this connection's MapSync. Native
        # block packets after this watermark are replayed at first ClientData.
        self.map_mutation_watermark: Optional[int] = None
        self.map_mutation_overflow: bool = False
        # Canonical VXL topology represented by this peer's MapSync payload.
        # Packet watermarks remain for compatibility with old embedders, but
        # real joins catch up from exact committed cells so replay cannot make
        # a collapse/damage effect produce a different result later.
        self.map_cell_watermark: Optional[int] = None
        self.map_cell_overflow: bool = False
        self.map_cell_replay: object | None = None
        # Frozen destroyed-cell masks represented by this connection's
        # MapSync snapshot. They are replayed only after the native GameScene
        # starts, in bounded batches, to clear stale retail VXL collision.
        self.map_air_replay: object | None = None
        # player_id -> concrete Player object/life token. The roster sent
        # before map loading can change while gameplay broadcasts are gated.
        self.known_player_lives: dict[int, tuple[int, int]] = {}
        # player_id -> life token whose KillAction was already queued. This is
        # separate from known_player_lives so Classic corpse reveal never
        # replays a death merely to repair a later packet-36 cleanup.
        self.known_player_deaths: dict[int, tuple[int, int]] = {}
        # player_id -> life token whose missed corpse explosion has already
        # been repaired with silent packet 36 for this GameScene.
        self.known_corpse_cleanups: dict[int, tuple[int, int]] = {}
        # Runtime entity IDs whose CreateEntity reached this exact GameScene.
        # A projectile may spawn while a peer is still loading and expire just
        # after it becomes in_game; sending DestroyEntity to that peer without
        # a matching create produces the retail "invalid entity on destroy"
        # warning and has crashed less forgiving entity classes.
        self.known_entity_ids: set[int] = set()

        # Packet waiting
        self._waiters: Dict[int, asyncio.Future] = {}
        # Armed only during a full map/mode replacement. The maintained retail
        # hook sends ClientInMenu(110) after it has installed LoadingMenu;
        # without this proof InitialInfo must never be sent into GameScene.
        self._scene_transition_ready: asyncio.Event | None = None
    
    def send(self, data: bytes, reliable: bool = True, prefix: int = 0x30):
        """Send packet to this connection."""
        import enet
        
        # Log raw packet (unless suppressed)
        packet_id = data[0] if len(data) > 0 else -1
        suppressed = packet_id in self.server.config.log_suppress_packets
        trace_packet = (
            not suppressed
            and bool(getattr(self.server.config, "packet_trace", False))
            and logger.isEnabledFor(logging.DEBUG)
        )
        
        if trace_packet:
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
        if trace_packet:
             comp_hex = ' '.join(f'{b:02X}' for b in prefixed_data)
             logger.debug(f"Compressed Hex Data: {comp_hex}")
        
        # Match the known-good retail transport path. ENet flag zero keeps
        # ordinary WorldUpdate packets sequenced but unreliable; reliable
        # delivery is reserved for explicit transitions and control traffic.
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
        """Normalize and store one complete selection for the first spawn."""
        from server.class_selection import normalize_server_selection

        selection = normalize_server_selection(
            self.server.config,
            packet.class_id,
            packet.loadout,
            packet.prefabs,
            packet.ugc_tools,
        )
        self.pending_selection = selection
        self.pending_class_id = selection.class_id
        self.pending_loadout = list(selection.loadout)
        self.pending_prefabs = list(selection.prefabs)
        self.pending_ugc_tools = list(selection.ugc_tools)

    def _resolve_join_team(self, wire_team: int) -> tuple[int, int]:
        """Resolve the initial team without coercing a spectator into Blue."""
        internal_team = wire_team_to_internal(wire_team)
        if internal_team == TEAM_SPECTATOR:
            from server.game_rules import get_rules

            if get_rules(self.server.config).enabled(
                "RULE_ENABLE_SPECTATORS"
            ):
                # Spectator is a real native team (wire id 0). Mode-specific
                # playable-team coercion must never turn it into a Character.
                return TEAM_SPECTATOR, WIRE_TEAM_SPECTATOR
            internal_team = None
        if internal_team is not None:
            prepare_team = getattr(self.server.mode, "prepare_join_team", None)
            if callable(prepare_team):
                internal_team = int(prepare_team(internal_team))
            return internal_team, internal_team_to_wire(internal_team)

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
        prepare_team = getattr(self.server.mode, "prepare_join_team", None)
        if callable(prepare_team):
            fallback_internal = int(prepare_team(fallback_internal))
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
            try:
                data = lzf_decompress(data[1:])
            except ValueError as exc:
                # A malformed stream is peer-local input. Reject it before a
                # packet decoder or the pre-join task can fail asynchronously.
                logger.warning(
                    "Rejected malformed LZF packet from %s: %s",
                    self.peer.address,
                    exc,
                )
                self.disconnect(reason=int(ERROR_DATA))
                return
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
        trace_packet = (
            not suppressed
            and bool(getattr(self.server.config, "packet_trace", False))
            and logger.isEnabledFor(logging.DEBUG)
        )
        if trace_packet:
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
        if (
            packet_id == 4
            and self.player is not None
            and not self.in_game
        ):
            try:
                reveal_complete = self.server.reveal_world_to(self)
                if reveal_complete is False:
                    # Exact pre-snapshot air repairs are intentionally bounded.
                    # Keep gameplay gated and continue on the next ClientData.
                    return
                self.in_game = True
                prune = getattr(self.server, "_prune_map_mutations", None)
                if prune is not None:
                    prune()
            except Exception:
                # Keep the connection gated so the next ClientData retries the
                # complete reveal instead of admitting a partially synced
                # client into the live broadcast stream.
                logger.exception("reveal_world_to failed")
                return

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

    def arm_scene_transition(self) -> None:
        """Require a fresh loader acknowledgement for the next scene epoch."""

        self.in_menu = False
        self._scene_transition_ready = asyncio.Event()

    def note_scene_transition_menu(self, in_menu: bool) -> None:
        """Accept packet 110 as ready only while a transition is armed."""

        if bool(in_menu) and self._scene_transition_ready is not None:
            self._scene_transition_ready.set()

    async def wait_for_scene_transition(self, timeout: float) -> bool:
        """Return whether the client entered LoadingMenu before ``timeout``."""

        ready = self._scene_transition_ready
        if ready is None:
            return False
        try:
            await asyncio.wait_for(ready.wait(), timeout=max(0.0, float(timeout)))
        except asyncio.TimeoutError:
            return False
        return True
    
    async def send_state_data(self, player_id: int = -1):
        """Send game state to newly joined player.

        The actual packet construction lives in server.builders.state_data
        so any per-mode/per-config field changes happen in one place.
        """
        from server.builders import build_state_data
        state = build_state_data(self.server, player_id=player_id)
        self.send(bytes(state.generate()), prefix=0x31)
        # Modes with a native pre-spawn menu contract may append small state
        # packets here.  Map Creator uses ForceTeamJoin(115) so the stock
        # LoadingMenu enters SelectPrefabs; sending it later (after ClientData)
        # is too late because the player has already been constructed.
        send_post_state = getattr(
            getattr(self.server, "mode", None), "send_post_state_data", None
        )
        if callable(send_post_state):
            send_post_state(self)
        self.state_sent = True

    async def send_skybox(self):
        """Send the active map's validated client environment resource."""
        world = getattr(self.server, "world_manager", None)
        metadata = getattr(world, "map_metadata", None)
        skybox_name = normalize_skybox_name(getattr(metadata, "skybox_name", None))
        if skybox_name is None:
            configured_default = getattr(
                self.server.config, "default_skybox", DEFAULT_SKYBOX_NAME
            )
            skybox_name = normalize_skybox_name(configured_default)
        if skybox_name is None:
            # Invalid administrator input must never become a native-client
            # resource path. This final value is a known stock retail asset.
            skybox_name = DEFAULT_SKYBOX_NAME
            logger.warning("Invalid default_skybox; using %s", skybox_name)

        skybox = SkyboxData()
        skybox.value = skybox_name
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

            live_character = bool(player.alive and player.spawned)
            spectator = int(player.team) == TEAM_SPECTATOR
            corpse_lifecycle = getattr(self.server, "corpse_lifecycle", None)
            active_for_join = getattr(
                corpse_lifecycle, "active_for_join", None
            )
            classic_corpse = bool(
                callable(active_for_join) and active_for_join(player)
            )
            if not live_character and not classic_corpse and not spectator:
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
            from server.roster import remember_player_life
            remember_player_life(self, player)
            color = SetColor()
            color.player_id = player.id
            color.value = int(player.block_color) & 0xFFFFFF
            self.send(bytes(color.generate()))

            if classic_corpse:
                # CreatePlayer establishes the player connection; KillAction
                # immediately changes its Character to ClassicCorpse. Packet
                # 36 cannot be used as creation and is intentionally absent.
                from server.roster import send_player_death

                send_player_death(self, player, self.server)
    
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
            packet = decode_runtime_packet(packet_id, data[1:])
            if packet is None:  # Defensive fallback; packet 13 has a runtime decoder.
                packet = SetClassLoadout(reader)
            self._cache_pre_join_loadout(packet)
            logger.debug(
                "Cached pre-join loadout: class_id=%s loadout=%s prefabs=%s ugc_tools=%s",
                packet.class_id,
                packet.loadout,
                packet.prefabs,
                packet.ugc_tools,
            )
        elif packet_id == SetColor.id:
            packet = SetColor(reader)
            # Retail constructs every colour-using tool before joining and
            # emits their defaults (observed: grey, red, then yellow).  These
            # packets are not user palette input.  The recovered server also
            # ignores SetColor until an alive player holds BLOCK_TOOL.
            logger.debug(
                "Ignored pre-join tool-constructor color: %#06x",
                int(packet.value) & 0xFFFFFF,
            )
        elif packet_id == ClockSync.id:
            packet = ClockSync(reader)
            self.send_clock_sync_response(packet.client_time)
        elif packet_id == ClientInMenu.id:
            packet = ClientInMenu(reader)
            self.in_menu = bool(packet.in_menu)
            self.note_scene_transition_menu(self.in_menu)
            logger.debug("Client pre-join menu state updated: in_menu=%s", self.in_menu)
        else:
            logger.debug(f"Unknown pre-join packet ID: {packet_id}")
    
    def reset_for_scene_reload(self) -> None:
        """Return an authenticated peer to the pre-join handshake state.

        Match transitions own removal of the old :class:`Player`; this method
        owns only connection-local loader state.  The ENet peer, Steam ticket,
        and XOR key deliberately survive so a map change can run
        ``InitialInfo -> MapSync -> StateData`` without a socket disconnect.
        It runs on the server event-loop thread while gameplay broadcasts are
        gated for this connection.
        """
        if self.player is not None:
            raise RuntimeError("scene reload requires the old Player to be detached")

        # A waiter belongs to the old scene/map epoch.  Letting its result
        # satisfy the next MapDataValidation would splice two VXL handshakes.
        for future in tuple(self._waiters.values()):
            if not future.done():
                future.cancel()
        self._waiters.clear()

        if self.reserved_player_id is not None:
            self.server.reserved_player_ids.discard(self.reserved_player_id)
        self.reserved_player_id = None
        self.map_sent = False
        self.state_sent = False
        self.pending_selection = None
        self.pending_class_id = None
        self.pending_loadout = []
        self.pending_prefabs = []
        self.pending_ugc_tools = []
        self.in_menu = None
        self.in_game = False
        self.map_mutation_watermark = None
        self.map_mutation_overflow = False
        self.map_cell_watermark = None
        self.map_cell_overflow = False
        self.map_cell_replay = None
        self.map_air_replay = None
        self.known_player_lives.clear()
        self.known_player_deaths.clear()
        self.known_corpse_cleanups.clear()
        self.known_entity_ids.clear()
        self._scene_transition_ready = None

    async def reload_scene(self) -> bool:
        """Stream the active map/mode into the existing authenticated peer.

        The transition service has already received ClientInMenu from the
        replacement loader before calling this method. The client must also
        answer the new ``InitialInfo`` with ``MapDataValidation``; ``False``
        lets the lifecycle retire only that timed-out peer.
        """
        self.reset_for_scene_reload()
        return await self.send_connection_data(require_map_validation=True)

    async def send_connection_data(
        self,
        *,
        require_map_validation: bool = False,
    ) -> bool:
        """Send the loader handshake and report whether map sync completed."""
        logger.info(f"Sending connection data to {self.peer.address}")
        
        # Send initial info
        await self.send_info()

        # UGC guests cannot participate in the ordinary CRC/MapSync handshake
        # until a lobby host has supplied packet 54/56/58 and populated the
        # native client's map_data buffer.  The isolated Map Creator server
        # deliberately acts as that host.  This hook is synchronous so the
        # MapDataValidation waiter below is installed before ENet can deliver
        # the client's reply on the next event-loop turn.
        pre_validation_transfer = getattr(
            getattr(self.server, "mode", None),
            "send_pre_validation_map_data",
            None,
        )
        if callable(pre_validation_transfer):
            pre_validation_transfer(self)
        
        # Send map data
        map_ready = await self.send_map_data(
            require_validation=require_map_validation,
        )
        if not map_ready:
            return False

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
        return True

    async def send_info(self):
        """Send InitialInfo (packet 114) to client.

        All field construction is in server.builders.initial_info — one
        place to drive map/mode/multipliers from real state.
        """
        from server.builders import build_initial_info
        packet = build_initial_info(self.server)
        # UGC distinguishes its first connected editor (host) from observing
        # clients in InitialInfo.  This must be per connection; mutating the
        # shared builder would accidentally promote every joiner to host.
        configure_for = getattr(
            getattr(self.server, "mode", None),
            "configure_initial_info_for",
            None,
        )
        if callable(configure_for):
            configure_for(self, packet)
        self.send(bytes(packet.generate()))
        logger.info(
            "Sent InitialInfo to %s map=%s mode_key=%d crc=%d",
            self.peer.address, packet.map_name, packet.mode_key, packet.checksum,
        )


    async def send_map_data(self, *, require_validation: bool = False) -> bool:
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
            if require_validation:
                # During an in-place scene reload, no reply means the native
                # client never entered LoadingMenu.  Do not pour VXL packets
                # into the old GameScene; that is a confirmed crash hazard.
                return False

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

        # Capture one immutable column set. World mutation and packet handling
        # share this event loop, and there is no await until the complete sync
        # is sent, so the following watermark is exactly contiguous with it.
        snapshot_columns = set(getattr(wm, "dirty_columns", ()) or ())

        if use_delta:
            payload = wm.serialize_dirty_columns_compressed(snapshot_columns)
            chunk_list = [
                payload[i:i + 1024] for i in range(0, len(payload), 1024)
            ]
            logger.info(
                "Map CRC match for %s (crc=%s): delta sync, %s dirty columns, %s bytes",
                self.peer.address,
                server_crc,
                len(snapshot_columns),
                len(payload),
            )
        else:
            # Full sync: stream the RAW .vxl bytes (native implicit-underground
            # encoding, ~0.5 MB) rather than re-serializing our filled in-memory
            # grid, which explicitly writes every underground voxel into a 36 MB
            # stream the strict client rejects (Steam join crash 2026-07-09).
            _iter_full = getattr(wm, "iter_full_sync_chunks", None)
            chunk_list = (
                _iter_full(snapshot_columns=snapshot_columns)
                if _iter_full is not None else None
            )
            if chunk_list is None:
                chunker = wm.get_chunker()
                if chunker is None:
                    logger.warning("No map chunker available for %s", self.peer.address)
                    return False
                chunk_list = list(chunker.iter())
            if client_crc is not None and not crc_match:
                logger.warning(
                    "Client/server map file CRC mismatch for %s: client=%s server=%s "
                    "— streaming full world state",
                    self.peer.address,
                    client_crc,
                    server_crc,
                )

        mark_snapshot = getattr(self.server, "mark_map_snapshot_complete", None)
        if mark_snapshot is not None:
            mark_snapshot(self)

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
        return True
    
    async def _on_new_player(self, packet: NewPlayerConnection):
        """Handle new player joining."""
        from server.player import Player
        from server.revival_master import (
            JoinTicketRejected,
            JoinTicketUnavailable,
            is_join_code,
        )
        from shared.constants import DISCONNECT

        revival_master = getattr(self.server, "revival_master", None)
        identity = None
        requested_name = str(packet.name or "")
        if is_join_code(requested_name):
            if revival_master is None:
                logger.warning(
                    "Rejecting join-code client %s: Revival bridge unavailable",
                    self.peer.address,
                )
                self.disconnect(reason=int(DISCONNECT.ERROR_TIMEOUT))
                return
            try:
                identity = await revival_master.consume_join_ticket(
                    requested_name
                )
            except JoinTicketRejected as error:
                logger.info(
                    "Rejected invalid Revival join code from %s: %s",
                    self.peer.address,
                    error,
                )
                self.disconnect(reason=int(DISCONNECT.ERROR_NOTICKET))
                return
            except JoinTicketUnavailable as error:
                logger.warning(
                    "Could not validate Revival join code from %s: %s",
                    self.peer.address,
                    error,
                )
                self.disconnect(reason=int(DISCONNECT.ERROR_TIMEOUT))
                return
            requested_name = identity.nickname
        elif bool(
            getattr(
                getattr(self.server.config, "revival", None),
                "require_identity",
                False,
            )
        ):
            logger.info(
                "Rejected unverified legacy player %s on identity-required server",
                self.peer.address,
            )
            self.disconnect(reason=int(DISCONNECT.ERROR_RANKED_SERVER))
            return

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
        is_spectator = internal_team == TEAM_SPECTATOR
        from server.player_names import allocate_unique_player_name

        player_name = allocate_unique_player_name(
            requested_name,
            self.server.players.values(),
        )
        if player_name != requested_name:
            logger.info(
                "Renamed duplicate/unsafe player %r to %r",
                requested_name,
                player_name,
            )
        player = Player(player_id, player_name, internal_team, weapon, self)
        if revival_master is not None:
            revival_master.bind_player(player, identity)
        player.local_language = int(packet.local_language)
        from server.class_selection import normalize_server_selection

        selection = self.pending_selection or normalize_server_selection(
            self.server.config,
            self.pending_class_id
            if self.pending_class_id is not None
            else packet.class_id,
            self.pending_loadout,
            self.pending_prefabs,
            self.pending_ugc_tools,
            fallback_class_id=packet.class_id,
        )
        prepare_selection = getattr(
            self.server.mode, "prepare_join_selection", None
        )
        if callable(prepare_selection) and not is_spectator:
            selection = prepare_selection(internal_team, selection)
        player.apply_class_selection(selection)

        # Some isolated rulesets need to choose the concrete tool used by the
        # very first Character life.  CreatePlayer must already contain that
        # decision; changing it from on_player_join is one packet too late and
        # briefly creates a model/tool combination the retail scene cannot
        # reconcile.  Ordinary modes have no hook and retain existing logic.
        prepare_spawn = getattr(
            self.server.mode, "prepare_player_spawn", None
        )
        if callable(prepare_spawn) and not is_spectator:
            prepare_spawn(player)
        
        self.player = player
        self.server.players[player_id] = player
        
        # Add to team
        if player.team in self.server.teams:
            self.server.teams[player.team].add_player(player)
        
        logger.info(
            "Player %s (ID %s, identity=%s) joined wire team %s as internal team %s",
            player.name,
            player_id,
            getattr(player, "identity_type", "legacy"),
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
        # Modes own their spawn policy. VIP in particular must enter directly
        # at the authored team base; bypassing this hook on the first life
        # made joins use the generic random terrain spawn while later lives
        # correctly used RoundLifecycle's mode-aware resolver.
        if is_spectator:
            # CreatePlayer still needs finite coordinates, but no server-side
            # Character life may be created for team 0. A normal team spawn is
            # a stable initial free-camera anchor understood by retail.
            spawn = self.server.world_manager.get_spawn_point(
                DEFAULT_WIRE_TEAM
            )
            sanitizer = getattr(
                self.server.world_manager,
                "sanitize_spawn_point",
                None,
            )
            if callable(sanitizer):
                spawn = sanitizer(spawn, DEFAULT_WIRE_TEAM)
            spawn = tuple(float(value) for value in spawn)
        else:
            from server.round_lifecycle import resolve_player_spawn

            spawn = resolve_player_spawn(self.server, player)
        orientation_resolver = getattr(
            self.server.mode, "get_spawn_orientation", None
        )
        if callable(orientation_resolver) and not is_spectator:
            player.set_orientation_vector(*orientation_resolver(player))
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
        create_packet.loadout = list(selection.loadout)
        create_packet.prefabs = list(selection.prefabs)

        # The joiner needs its OWN CreatePlayer to bind its local player to the
        # server id — deliver it directly (one packet is safe mid-transition).
        # Other players only learn about the joiner once THEY'RE in-game, so
        # broadcast (gameplay-gated) reaches just settled clients.
        create_bytes = bytes(create_packet.generate())
        self.send(create_bytes)
        self.server.broadcast(create_bytes, exclude=player)

        # CreatePlayer carries no block palette. Publish it immediately after
        # character creation to both the joining client and settled peers;
        # otherwise their first remote BlockLine resolves through a default
        # colour that can differ from the authoritative Player state.
        color_packet = SetColor()
        color_packet.player_id = player.id
        color_packet.value = int(player.block_color) & 0xFFFFFF
        color_bytes = bytes(color_packet.generate())
        self.send(color_bytes)
        self.server.broadcast(color_bytes, exclude=player)
        
        # Team 0 is a roster/camera state, not a Character simulation state.
        # Calling Player.spawn here is the exact bug that produced a visible
        # Blue paratrooper when the client asked to spectate.
        if is_spectator:
            player.set_position(spawn[0], spawn[1], spawn[2])
            player.alive = False
            player.spawned = False
            player.death_time = 0.0
        else:
            player.spawn(spawn[0], spawn[1], spawn[2])
        from server.roster import remember_player_life
        remember_player_life(self, player)
        # broadcast() delivered CreatePlayer only to settled connections.
        # Record exactly those recipients so first-frame catch-up can identify
        # clients that were still gated and therefore missed this new life.
        for other in self.server.connections.values():
            if other is self or not getattr(other, "in_game", False):
                continue
            remember_player_life(other, player)
        if not is_spectator:
            self._send_spawn_hp()
        if getattr(self.server, 'debug_parity', None) is not None:
            self.server.debug_parity.on_player_join(player)
        
        # Notify game mode
        if self.server.mode and not is_spectator:
            await self.server.mode.on_player_join(player)

        # NB: the live roster + map entities are revealed to this client on its
        # FIRST ClientData (server.reveal_world_to), i.e. once it's actually in
        # the GameScene — never in the join handshake (that floods the
        # transition and crashes the client). See on_receive / reveal_world_to.
