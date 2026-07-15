"""
A2S Steam Query Protocol Handler
Allows the server to appear in Steam's server browser and LAN discovery.

Runs as a separate asyncio UDP server to avoid breaking ENet.
- A2S_INFO, A2S_PLAYER, A2S_RULES queries
- LAN discovery (HELLO, HELLOLAN)
"""

import asyncio
import struct
import json
import random
import logging
from typing import TYPE_CHECKING, Optional, Tuple, Dict

from .game_constants import TEAM1, TEAM2
from .mode_data import get as get_mode_data

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    import enet

logger = logging.getLogger(__name__)


class A2SProtocol(asyncio.DatagramProtocol):
    """Asyncio UDP protocol for A2S queries."""
    
    def __init__(self, handler: 'A2SHandler'):
        self.handler = handler
        self.transport = None
    
    def connection_made(self, transport):
        self.transport = transport
    
    def datagram_received(self, data: bytes, addr: tuple):
        """Handle incoming UDP datagram."""
        response = self.handler.handle_packet(data, addr)
        if response:
            self.transport.sendto(response, addr)


class A2SConstants:
    """A2S Protocol constants."""
    PREFIX_BYTES = b'\xff\xff\xff\xff'
    
    # Request headers
    A2S_INFO_REQUEST = 0x54
    A2S_PLAYER_REQUEST = 0x55
    A2S_RULES_REQUEST = 0x56
    A2S_SERVERQUERY_GETCHALLENGE = 0x57
    
    # Response headers
    A2S_INFO_RESPONSE = 0x49
    A2S_PLAYER_RESPONSE = 0x44
    A2S_RULES_RESPONSE = 0x45
    A2S_CHALLENGE_RESPONSE = 0x41
    
    # Query string
    QUERY_STRING = b"Source Engine Query\0"
    
    # Extra Data Flags
    EDF_PORT = 0x80
    EDF_STEAM_ID = 0x10
    EDF_SOURCE_TV = 0x40
    EDF_KEYWORDS = 0x20
    EDF_GAME_ID = 0x01


class A2SHandler:
    """
    A2S Query handler - runs as separate asyncio UDP server.
    Handles Steam server browser queries and LAN discovery.
    NOTE: Cannot share same port as ENet, so runs on port+1.
    """
    
    def __init__(self, server: 'BattleSpadesServer'):
        self.server = server
        self.challenge = self._generate_challenge()
        self._challenge_counter = 0
        self._transport = None
        self._protocol = None
        self._running = False
    
    def _generate_challenge(self) -> int:
        """Generate a new random challenge value."""
        return random.randint(-2147483648, 2147483647)
    
    def update(self):
        """Periodic update - refresh challenge occasionally."""
        self._challenge_counter += 1
        if self._challenge_counter >= 18000:  # ~5 minutes at 60 ticks
            self.challenge = self._generate_challenge()
            self._challenge_counter = 0
    
    def intercept(self, address, data: bytes):
        """
        Intercept raw UDP packets before ENet processes them.
        Called by ENet's intercept callback.
        """
        if not data:
            return
        
        response = None
        
        # LAN discovery - HELLO
        if data == b'HELLO':
            logger.debug(f"LAN HELLO from {address}")
            response = b'HI'
        
        # LAN discovery - HELLOLAN (JSON server info)
        elif data == b'HELLOLAN':
            logger.debug(f"LAN HELLOLAN from {address}")
            response = self._make_lan_info()
        
        # A2S Steam protocol - must start with 0xFFFFFFFF
        elif len(data) >= 5 and data[:4] == A2SConstants.PREFIX_BYTES:
            logger.debug(f"A2S query from {address} (header: 0x{data[4]:02x})")
            response = self._handle_a2s_request(data)
        
        # Send response if we have one
        if response and self.server.host and self.server.host.socket:
            try:
                self.server.host.socket.send(address, response)
                logger.debug(f"Sent {len(response)} bytes to {address}")
            except Exception as e:
                logger.error(f"Failed to send response to {address}: {e}")
    
    async def _start_udp_server(self):
        """Create and start the UDP server."""
        import asyncio
        import socket
        
        # Use same port as game server - we'll try to create a second socket
        # This may not work, in which case A2S won't be available
        port = self.server.config.port
        
        try:
            loop = asyncio.get_event_loop()
            
            # Create a UDP socket that can coexist with ENet
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except AttributeError:
                pass  # Windows doesn't have SO_REUSEPORT
            sock.setblocking(False)
            sock.bind(('0.0.0.0', port))
            
            self._transport, self._protocol = await loop.create_datagram_endpoint(
                lambda: A2SProtocol(self),
                sock=sock
            )
            self._running = True
            logger.info(f"A2S UDP server started on port {port}")
        except OSError as e:
            logger.warning(f"Could not start A2S server on port {port}: {e}")
            logger.warning("A2S/LAN discovery will not be available")
    
    def stop(self):
        """Stop the A2S UDP server."""
        if self._transport:
            self._transport.close()
            self._transport = None
        self._running = False
    
    def handle_packet(self, data: bytes, addr: tuple) -> Optional[bytes]:
        """Handle incoming packet and return response if any."""
        if not data:
            return None
        
        # LAN discovery - HELLO
        if data == b'HELLO':
            logger.debug(f"LAN HELLO from {addr}")
            return b'HI'
        
        # LAN discovery - HELLOLAN (JSON server info)
        if data == b'HELLOLAN':
            logger.debug(f"LAN HELLOLAN from {addr}")
            return self._make_lan_info()
        
        # A2S Steam protocol - must start with 0xFFFFFFFF
        if len(data) >= 5 and data[:4] == A2SConstants.PREFIX_BYTES:
            logger.debug(f"A2S query from {addr} (header: 0x{data[4]:02x})")
            return self._handle_a2s_request(data)
        
        return None
    
    def _make_lan_info(self) -> bytes:
        """Create LAN discovery JSON response."""
        config = self.server.config
        map_name = self.server.world_manager.map_name if self.server.world_manager else config.map_name
        
        entry = {
            "name": config.server_name,
            "players_current": len(self.server.players),
            "players_max": config.max_players,
            "map": map_name,
            "game_mode": config.game_mode,
            "game_version": "1.0a1"
        }
        
        return json.dumps(entry).encode()
    
    def _decode_request(self, data: bytes) -> Optional[Tuple[int, int]]:
        """Decode A2S request. Returns (header, challenge) or None."""
        if len(data) < 5:
            return None
        
        if data[:4] != A2SConstants.PREFIX_BYTES:
            return None
        
        header = data[4]
        
        # Master server challenge
        if header == A2SConstants.A2S_SERVERQUERY_GETCHALLENGE:
            return (header, -1)
        
        # A2S_INFO
        if header == A2SConstants.A2S_INFO_REQUEST:
            if len(data) == 5:  # Broadcast
                return (header, self.challenge)
            if len(data) < 25:
                return None
            if data[5:25] != A2SConstants.QUERY_STRING:
                return None
            challenge = -1
            if len(data) >= 29:
                challenge = struct.unpack("<i", data[25:29])[0]
            return (header, challenge)
        
        # A2S_PLAYER or A2S_RULES
        if header in (A2SConstants.A2S_PLAYER_REQUEST, A2SConstants.A2S_RULES_REQUEST):
            challenge = -1
            if len(data) >= 9:
                challenge = struct.unpack("<i", data[5:9])[0]
            return (header, challenge)
        
        return None
    
    def _handle_a2s_request(self, data: bytes) -> Optional[bytes]:
        """Handle A2S request and return response bytes."""
        result = self._decode_request(data)
        if not result:
            return None
        
        header, req_challenge = result
        
        # Broadcast discovery
        if len(data) == 5 and header == A2SConstants.A2S_INFO_REQUEST:
            return self._make_info_response()
        
        # Master server challenge
        if header == A2SConstants.A2S_SERVERQUERY_GETCHALLENGE:
            return self._make_challenge_response()
        
        # Need challenge first
        if req_challenge == -1:
            return self._make_challenge_response()
        
        # Validate challenge
        if req_challenge != self.challenge:
            return self._make_challenge_response()
        
        if header == A2SConstants.A2S_INFO_REQUEST:
            logger.debug("Sending INFO response")
            return self._make_info_response()
        elif header == A2SConstants.A2S_PLAYER_REQUEST:
            logger.debug("Sending PLAYER response")
            return self._make_player_response()
        elif header == A2SConstants.A2S_RULES_REQUEST:
            logger.debug("Sending RULES response")
            return self._make_rules_response()
        
        return None
    
    def _make_challenge_response(self) -> bytes:
        """Create A2S_CHALLENGE response."""
        return A2SConstants.PREFIX_BYTES + bytes([A2SConstants.A2S_CHALLENGE_RESPONSE]) + struct.pack("<i", self.challenge)
    
    def _make_info_response(self) -> bytes:
        """Create A2S_INFO response with live server data."""
        config = self.server.config
        
        packet = bytearray(A2SConstants.PREFIX_BYTES)
        packet.append(A2SConstants.A2S_INFO_RESPONSE)
        packet.append(168)  # Protocol version
        
        # Server name
        packet.extend(config.server_name.encode('utf-8', 'replace') + b'\0')
        
        # Map name
        map_name = self.server.world_manager.map_name if self.server.world_manager else config.map_name
        packet.extend(map_name.encode('utf-8', 'replace') + b'\0')
        
        # Game directory
        packet.extend(b"aceofspades\0")
        
        # Game name
        packet.extend(b"AoS\0")
        
        # App ID (short) - full ID in EDF
        packet.extend(struct.pack("<h", 0))
        
        # Player counts
        packet.append(len(self.server.players))
        packet.append(config.max_players)
        packet.append(sum(
            1 for player in self.server.players.values()
            if bool(getattr(player, "is_bot", False))
        ))
        
        # Server type: 'd' = dedicated
        packet.append(ord('d'))
        
        # OS: 'l' = linux (what original uses)
        packet.append(ord('l'))
        
        # Password protected
        packet.append(0)
        
        # VAC secured (original uses 1)
        packet.append(1)
        
        # Version
        packet.extend(b"1.0.0.0\0")
        
        # EDF - must include STEAM_ID like original
        edf = A2SConstants.EDF_PORT | A2SConstants.EDF_STEAM_ID | A2SConstants.EDF_KEYWORDS | A2SConstants.EDF_GAME_ID
        packet.append(edf)
        
        # Port (EDF_PORT)
        packet.extend(struct.pack("<H", config.port))
        
        # Steam ID (EDF_STEAM_ID) - 64-bit, must come before keywords
        packet.extend(struct.pack("<q", 224540))
        
        # Keywords (EDF_KEYWORDS)
        active_mode = get_mode_data(config.game_mode)
        tags = [
            "v168",
            "playlist=8",
            f"mode={int(active_mode.mode_id):04d}",
        ]
        if active_mode.classic:
            tags.append("classic")
        keywords = ";".join(tags)
        packet.extend(keywords.encode('utf-8', 'replace') + b'\0')
        
        # Game ID (EDF_GAME_ID) - 64-bit
        packet.extend(struct.pack("<q", 224540))
        
        return bytes(packet)
    
    def _make_player_response(self) -> bytes:
        """Create A2S_PLAYER response."""
        packet = bytearray(A2SConstants.PREFIX_BYTES)
        packet.append(A2SConstants.A2S_PLAYER_RESPONSE)
        
        players = list(self.server.players.values())
        packet.append(len(players))
        
        for idx, player in enumerate(players):
            packet.append(idx)
            name = player.name if player.name else f"Player{player.id}"
            packet.extend(name.encode('utf-8', 'replace') + b'\0')
            score = getattr(player, 'kills', 0)
            packet.extend(struct.pack("<i", score))
            duration = getattr(player, 'time_connected', 0.0)
            packet.extend(struct.pack("<f", duration))
        
        return bytes(packet)
    
    def _make_rules_response(self) -> bytes:
        """Create A2S_RULES response."""
        config = self.server.config
        
        rules = {
            "mode": config.game_mode,
            "map": config.map_name,
            "friendly_fire": "1" if config.friendly_fire else "0",
            "fall_damage": "1" if config.fall_damage else "0",
            "respawn_time": str(int(config.respawn_time)),
            "score_limit": str(config.score_limit),
            "team1": config.team1_name,
            "team2": config.team2_name,
        }
        
        # Team scores
        if TEAM1 in self.server.teams:
            rules["team1_score"] = str(self.server.teams[TEAM1].score)
        if TEAM2 in self.server.teams:
            rules["team2_score"] = str(self.server.teams[TEAM2].score)
        
        packet = bytearray(A2SConstants.PREFIX_BYTES)
        packet.append(A2SConstants.A2S_RULES_RESPONSE)
        packet.extend(struct.pack("<h", len(rules)))
        
        for key, value in rules.items():
            packet.extend(str(key).encode('utf-8', 'replace') + b'\0')
            packet.extend(str(value).encode('utf-8', 'replace') + b'\0')
        
        return bytes(packet)
