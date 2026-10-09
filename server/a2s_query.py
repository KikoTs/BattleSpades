"""
A2S Steam Query Protocol Handler
Allows the server to appear in Steam's server browser and LAN discovery.

Uses the ENet intercept on the game port, plus an optional Steam LAN query
socket on 27015..27020 when the game port is outside that range.
- A2S_INFO, A2S_PLAYER, A2S_RULES queries
- LAN discovery (HELLO, HELLOLAN)
"""

import asyncio
import struct
import json
import random
import logging
import sys
import time
import socket
from typing import TYPE_CHECKING, Optional, Tuple, Dict

from .game_constants import TEAM1, TEAM2
from shared.lan_discovery import STEAM_LAN_PORTS
from .mode_data import get as get_mode_data
from .steam_master import (
    build_game_tags,
    server_population,
    STEAM_APP_ID,
    STEAM_DESCRIPTION,
)

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
        # HELLOLAN clients join the reply's source port, so that protocol
        # must only answer on the actual ENet game socket.
        if not data.startswith(A2SConstants.PREFIX_BYTES):
            return
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
    Shared responses for game-port queries and Steam LAN discovery.
    The extra query socket never binds over ENet or the Steam registrar.
    """
    
    def __init__(self, server: 'BattleSpadesServer'):
        self.server = server
        self.challenge = self._generate_challenge()
        self._challenge_counter = 0
        self._transport = None
        self._protocol = None
        self._running = False
        self.lan_query_port: Optional[int] = None
        # id(player) -> (player, monotonic first-seen). Player has no join
        # timestamp, so A2S_PLAYER durations are measured from the first
        # roster sweep that saw each player (update() sweeps every second).
        self._joined_at: Dict[int, Tuple[object, float]] = {}
    
    def _generate_challenge(self) -> int:
        """Generate a new random challenge value."""
        return random.randint(-2147483648, 2147483647)
    
    def update(self):
        """Periodic update - refresh challenge occasionally."""
        self._challenge_counter += 1
        if self._challenge_counter % 60 == 0:
            self._track_join_times()
        if self._challenge_counter >= 18000:  # ~5 minutes at 60 ticks
            self.challenge = self._generate_challenge()
            self._challenge_counter = 0

    def _track_join_times(self) -> Dict[int, float]:
        """Record first-seen times for the roster and forget departed players."""
        now = time.monotonic()
        players = tuple((getattr(self.server, "players", None) or {}).values())
        live = {id(player): player for player in players}
        joined = {
            key: entry for key, entry in self._joined_at.items()
            if live.get(key) is entry[0]
        }
        for key, player in live.items():
            if key not in joined:
                joined[key] = (player, now)
        self._joined_at = joined
        return {key: now - entry[1] for key, entry in joined.items()}
    
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
    
    async def start(self) -> None:
        """Expose A2S on a Steam LAN scan port without sharing game sockets."""
        config = self.server.config
        if self._running or not config.lan_discovery:
            return
        if config.port in STEAM_LAN_PORTS:
            self.lan_query_port = config.port
            self._running = True
            logger.info("Steam LAN discovery uses game UDP port %d", config.port)
            return

        reserved = set()
        if config.steam.enabled:
            reserved.update((config.steam.steam_port,
                             config.steam.effective_query_port(config.port)))
        loop = asyncio.get_running_loop()
        for port in STEAM_LAN_PORTS:
            if port in reserved:
                continue
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # Never steal datagrams from another local game server.
                if sys.platform == "win32":
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.setblocking(False)
                sock.bind(('0.0.0.0', port))
                self._transport, self._protocol = await loop.create_datagram_endpoint(
                    lambda: A2SProtocol(self), sock=sock,
                )
            except OSError as exc:
                logger.debug("Steam LAN query port %d unavailable: %s", port, exc)
                continue
            finally:
                # On success the asyncio transport owns the socket. Also
                # close on cancellation or failure to create the endpoint.
                if self._transport is None:
                    sock.close()
            self.lan_query_port = port
            self._running = True
            logger.info("Steam LAN discovery listening on UDP %d (game UDP %d)",
                        port, config.port)
            return
        logger.warning("Steam LAN discovery unavailable: UDP 27015-27020 are busy. "
                       "Direct connections and game-port A2S still work on UDP %d.",
                       config.port)
    
    def stop(self) -> None:
        """Close the extra query socket; ENet owns the game-port listener."""
        if self._transport:
            self._transport.close()
            self._transport = None
        self._protocol = None
        self.lan_query_port = None
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
        
        population = server_population(self.server)
        entry = {
            "name": config.server_name,
            "players_current": population.players,
            "players_max": population.max_players,
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
        
        # Game description recovered from the retail Steam wrapper.
        packet.extend(STEAM_DESCRIPTION.encode("ascii") + b"\0")
        
        # A2S keeps a historical uint16 AppID plus the full uint64 GameID EDF.
        packet.extend(struct.pack("<H", STEAM_APP_ID & 0xFFFF))
        
        # Player counts. Source convention: ``players`` includes ``bots``
        # (the Revival master and Steam sidecar derive humans as the
        # difference), and loading players already hold a slot.
        population = server_population(self.server)
        packet.append(population.players)
        packet.append(population.max_players)
        packet.append(population.bots)
        
        # Server type: 'd' = dedicated
        packet.append(ord('d'))
        
        # Host OS, as required by Source query protocol.
        os_code = "w" if sys.platform == "win32" else (
            "m" if sys.platform == "darwin" else "l"
        )
        packet.append(ord(os_code))
        
        # Password protected
        join_password = getattr(config, "join_password", "")
        packet.append(1 if isinstance(join_password, str) and join_password else 0)
        
        # VAC is truthful: anonymous public listing defaults to insecure mode.
        steam_master = getattr(self.server, "steam_master", None)
        packet.append(1 if bool(getattr(steam_master, "secure", False)) else 0)
        
        # Version
        packet.extend(config.steam.game_version.encode('ascii', 'replace') + b'\0')
        
        # EDF - must include STEAM_ID like original
        edf = A2SConstants.EDF_PORT | A2SConstants.EDF_STEAM_ID | A2SConstants.EDF_KEYWORDS | A2SConstants.EDF_GAME_ID
        packet.append(edf)
        
        # Port (EDF_PORT)
        packet.extend(struct.pack("<H", config.port))
        
        # Steam ID (EDF_STEAM_ID) - Valve-assigned when registered, otherwise
        # the stable non-Steam identity used by InitialInfo.
        steam_id = int(getattr(steam_master, "steam_id", 0) or config.steam_id)
        packet.extend(struct.pack("<Q", steam_id & 0xFFFFFFFFFFFFFFFF))
        
        # Keywords (EDF_KEYWORDS). ``mode=`` in the retail tags is the
        # SERVERMODE browser category, so the AoSPlay master reads the
        # gameplay mode from the trailing ``gamemode=`` keyword. Retail
        # scans tags by prefix and ignores it; the Steam tags stay retail.
        keywords = build_game_tags(config)
        gameplay = f"{keywords};gamemode={get_mode_data(config.game_mode).code}"
        if len(gameplay.encode('utf-8')) < 128:
            keywords = gameplay
        packet.extend(keywords.encode('utf-8', 'replace') + b'\0')
        
        # Game ID (EDF_GAME_ID) - 64-bit
        packet.extend(struct.pack("<Q", STEAM_APP_ID))
        
        return bytes(packet)
    
    def _make_player_response(self) -> bytes:
        """Create A2S_PLAYER response."""
        packet = bytearray(A2SConstants.PREFIX_BYTES)
        packet.append(A2SConstants.A2S_PLAYER_RESPONSE)
        
        durations = self._track_join_times()
        players = sorted(
            (getattr(self.server, "players", None) or {}).values(),
            key=lambda player: int(getattr(player, "id", 0)),
        )[:255]
        packet.append(len(players))

        for idx, player in enumerate(players):
            packet.append(idx)
            name = player.name if player.name else f"Player{player.id}"
            packet.extend(name.encode('utf-8', 'replace') + b'\0')
            # The in-game scoreboard's SCORE column, not raw kills.
            try:
                score = int(getattr(player, 'score', 0) or 0)
            except (TypeError, ValueError):
                score = 0
            score = max(-2147483648, min(2147483647, score))
            packet.extend(struct.pack("<i", score))
            packet.extend(struct.pack("<f", float(durations.get(id(player), 0.0))))
        
        return bytes(packet)
    
    def _make_rules_response(self) -> bytes:
        """Create A2S_RULES response."""
        config = self.server.config
        
        rules = {
            "mode": config.game_mode,
            # The live map, not the configured first map of the rotation.
            "map": str(
                getattr(getattr(self.server, "world_manager", None), "map_name", "")
                or config.map_name
            ),
            "friendly_fire": "1" if config.friendly_fire else "0",
            "fall_damage": "1" if config.fall_damage else "0",
            "respawn_time": str(int(config.respawn_time)),
            # The active mode owns its limit (TDM 200, CTF 10, ...); the
            # generic game.score_limit is only a CTF-era default.
            "score_limit": str(int(getattr(
                getattr(self.server, "mode", None),
                "score_limit",
                config.score_limit,
            ))),
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
