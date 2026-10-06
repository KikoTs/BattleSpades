"""Optional player-hosted Steam transport for patched retail clients.

The helper owns Steam and the UDP bridge. Only its authenticated private IPC
can associate a loopback ENet endpoint with a Steam identity. Direct clients
retain the existing IP identity and admission rules.
"""
from __future__ import annotations

import asyncio
import logging
import json
import sys
import uuid
from pathlib import Path

from client_patches.retail_mousefix.aos_steam_bridge import (
    BridgeClient, decode, encode, integer, steam_id, local_host_path,
)
from server.mode_data import get as get_mode_data
from server.steam_master import server_population

logger = logging.getLogger(__name__)


def endpoint(peer) -> tuple[str, int] | None:
    """Read the native address without retaining a borrowed address wrapper."""
    try:
        address = peer.address
        host = address.host
        if isinstance(host, bytes):
            host = host.decode('ascii')
        return str(host), int(address.port)
    except (AttributeError, ValueError, UnicodeError):
        return None


class SteamP2PService:
    def __init__(self, server) -> None:
        self.server = server
        self.bridge: BridgeClient | None = None
        self.task: asyncio.Task | None = None
        self.routes: dict[int, tuple[str, str]] = {}
        self.peers: dict[object, tuple[str, str]] = {}
        self.instance = uuid.uuid4().hex
        self.hosted = False
        self.stopping = False
        self.local_record: Path | None = None

    def _write_local_record(self) -> None:
        if self.local_record is not None:
            try:
                self.local_record.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.local_record.with_suffix('.' + self.instance + '.tmp')
                temporary.write_text(json.dumps({'instance': self.instance, 'port': self.server.config.port}), encoding='ascii')
                temporary.replace(self.local_record)
            except OSError:
                logger.warning('Could not refresh the local-host shortcut; remote relay hosting continues')

    def identity_for(self, peer) -> str | None:
        """Pin an identity to the ENet peer, even after its UDP port retires."""
        # Dedicated-server relay hosts (server/steam_host.py) share this lookup,
        # so bans, vote kicks and lockouts see ``steam:<id>`` for their players.
        host = getattr(self.server, 'steam_host', None)
        if host is not None:
            identity = host.identity_for(peer)
            if identity is not None:
                return identity
        if not self.routes and not self.peers:
            return None
        cached = self.peers.get(peer)
        if cached is not None:
            return 'steam:' + cached[1]
        address = endpoint(peer)
        if address is None or address[0] != '127.0.0.1':
            return None
        route = self.routes.get(address[1])
        if route is not None:
            self.peers[peer] = route
            return 'steam:' + route[1]
        return None

    def forget_peer(self, peer) -> None:
        self.peers.pop(peer, None)
        host = getattr(self.server, 'steam_host', None)
        if host is not None:
            host.forget_peer(peer)

    def metadata(self) -> list[str]:
        config = self.server.config
        mode = get_mode_data(config.game_mode)
        world = getattr(self.server, 'world_manager', None)
        name = str(getattr(world, 'map_name', '') or config.map_name)
        count = server_population(self.server)
        skin = getattr(getattr(config, 'steam', None), 'texture_skin', '') or ('mafia' if mode.mafia else '')
        # Clip by UTF-8 byte length without cutting a codepoint. Metadata is UI
        # text only; the normal retail handshake remains authoritative.
        def text(value: str, size: int) -> str:
            clean = ''.join(c for c in str(value) if ord(c) >= 32 and ord(c) != 127)
            return encode(clean.encode('utf-8')[:size].decode('utf-8', 'ignore'))
        wire_mode = mode.code[1:] if mode.classic and mode.code.startswith('c') else mode.code
        return [text(config.server_name, 96), text(name, 96), text(wire_mode, 16),
                str(count.max_players), str(count.players), str(int(bool(config.join_password))),
                str(int(bool(mode.classic))), text(skin, 32)]

    async def start(self) -> None:
        config = self.server.config
        if not getattr(config, 'steam_p2p_enabled', False):
            return
        if sys.platform != 'win32':
            raise ValueError('The retail Steam helper currently supports Windows hosts only')
        if getattr(getattr(config, 'revival', None), 'require_identity', False):
            raise ValueError('Retail Steam hosting cannot yet satisfy revival.require_identity; use a separate unranked server configuration')
        configured = getattr(config, 'steam_p2p_bridge', None)
        root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
        if configured:
            executable = Path(configured)
        else:
            candidates = [root / 'relay' / 'aos-retail-relay.exe',
                          Path(getattr(sys, '_MEIPASS', root)) / 'relay' / 'aos-retail-relay.exe',
                          root / 'out' / 'retail-mousefix' / 'relay' / 'aos-retail-relay.exe']
            executable = next((path for path in candidates if path.is_file()), candidates[0])
        if not executable.is_file():
            raise FileNotFoundError(f'Retail Steam helper missing: {executable}. Use --steam-p2p-bridge PATH')
        self.bridge = BridgeClient(str(executable), logger.warning)
        self.bridge.start()
        self.task = asyncio.create_task(self._run(), name='retail-steam-p2p')

    def _drop(self, connection: str) -> None:
        self.routes = {port: value for port, value in self.routes.items() if value[0] != connection}
        for peer, value in tuple(self.peers.items()):
            if value[0] == connection:
                client = self.server.connections.get(peer)
                if client is not None:
                    client.disconnect(reason=8)

    def event(self, fields: list[str]) -> None:
        bridge = self.bridge
        if bridge is None:
            return
        if fields[0] == 'READY':
            config = self.server.config
            bridge.send('HOST', '1', str(config.port), str(getattr(config, 'steam_p2p_port', 168)),
                        self.instance, '0' if getattr(config, 'steam_p2p_private', False) else '1', *self.metadata())
        elif fields[0] == 'HOSTED' and len(fields) == 5:
            self.hosted = True
            self.local_record = Path(local_host_path(fields[3], fields[4]))
            self._write_local_record()
            logger.info('Steam relay hosting ready: lobby=%s virtual_port=%s', fields[2], fields[4])
        elif fields[0] == 'PEER' and len(fields) == 4:
            connection = str(integer(fields[1], 1))
            port = integer(fields[2], 1, 65535)
            identity = steam_id(fields[3])
            key = 'steam:' + identity
            kicked = getattr(getattr(self.server, 'vote_manager', None), 'match_kick_reason', lambda _: None)(key)
            if port in self.routes or self.server.ban_manager.is_banned(key) is not None or kicked is not None:
                bridge.send('DENY', connection)
                return
            self.routes[port] = (connection, identity)
            # This ACK is the barrier before any gameplay datagram reaches ENet.
            bridge.send('ALLOW', connection)
        elif fields[0] == 'DROP' and len(fields) == 3:
            self._drop(fields[1])
        elif fields[0] == 'ERROR' and len(fields) == 3:
            raise RuntimeError(decode(fields[2], 1024))

    async def _run(self) -> None:
        while not self.stopping:
            await self._serve_helper()
            if self.stopping:
                break
            logger.info('Retrying Steam hosting in 10 seconds; active relay clients must rejoin')
            await asyncio.sleep(10)
            self.instance = uuid.uuid4().hex
            self.local_record = None
            try:
                assert self.bridge is not None
                self.bridge.start()
            except OSError:
                logger.exception('Could not restart the Steam helper')
                break

    async def _serve_helper(self) -> None:
        assert self.bridge is not None
        try:
            next_metadata = asyncio.get_running_loop().time() + 5
            while not self.stopping:
                for fields in self.bridge.poll():
                    self.event(fields)
                self.bridge.reap()
                now = asyncio.get_running_loop().time()
                if self.hosted and now >= next_metadata:
                    self.bridge.send('META', *self.metadata())
                    self._write_local_record()
                    next_metadata = now + 5
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Steam relay hosting stopped; direct server remains available')
        finally:
            self.hosted = False
            for connection, unused in tuple(self.routes.values()):
                self._drop(connection)
            self.bridge.close()
            await asyncio.sleep(0.2)
            self.bridge.reap(force=True)
            if self.local_record is not None:
                try:
                    if json.loads(self.local_record.read_text(encoding='ascii')).get('instance') == self.instance:
                        self.local_record.unlink()
                except (OSError, ValueError):
                    pass

    async def close(self) -> None:
        self.stopping = True
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self.bridge is not None:
            self.bridge.close()
            # Give Steam a bounded chance to remove the advertisement.
            for _ in range(20):
                self.bridge.reap()
                if not self.bridge.pending:
                    break
                await asyncio.sleep(0.05)
            self.bridge.reap(force=True)
        self.routes.clear()
        self.peers.clear()
