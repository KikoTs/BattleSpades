# -*- coding: utf-8 -*-
"""
testbot.client — minimal ENet client + wire layer using the ORIGINAL game's
compiled modules from G:\\AoSRevival\\aceofspades_nonsteam\\.

Python 2.7 32-bit only.

The wire layer is shaped to match BattleSpades server (server\\connection.py):
- Outbound: chr(prefix) + lzf_chunk(bytes(packet.generate()))
- Inbound:  if data[0] == 0x31: lzf_decompress_client(data[1:]); else data[1:]
            then XOR-decrypt with steam_key if set
            then first byte = packet id, rest is body
- Connect data byte = PROTOCOL_VERSION (168)
"""
from __future__ import print_function

import json
import os
import sys
import time

# Make the originals importable. Put nonsteam ahead of everything so we
# definitely use the original .pyds, not anything in BattleSpades.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
NONSTEAM = os.path.normpath(os.path.join(REPO_ROOT, '..', 'aceofspades_nonsteam'))
if NONSTEAM not in sys.path:
    sys.path.insert(0, NONSTEAM)

import enet  # noqa: E402
import shared.packet as P  # noqa: E402
import shared.bytes as B  # noqa: E402
import shared.lzf as L  # noqa: E402
from shared.constants import PROTOCOL_VERSION, MAX_PACKET_DECOMPRESSION_SIZE  # noqa: E402


# ----- Wire-layer helpers --------------------------------------------------

def lzf_chunk(s):
    """The 'fake LZF' chunking format that BattleSpades server uses on send.
    Matches server\\util.py byte-for-byte."""
    result = bytearray()
    while len(s) > 32:
        result.extend(b'\x1F' + s[:32])
        s = s[32:]
    if len(s) > 0:
        result.extend(chr(len(s) - 1) + s)
    return bytes(result)


def encode(packet, prefix=0x30):
    """Serialize a shared.packet.<Cls> instance to wire bytes.

    Asymmetry note: BattleSpades server's RECEIVE side only un-chunks when
    the prefix byte is 0x31; it treats 0x30 payloads as raw. Its SEND side
    always chunks regardless of prefix. So we mirror that on the bot:
      - prefix 0x30 -> raw body (no chunking)
      - prefix 0x31 -> chunked body
    The server's send always chunks; we always un-chunk on receive.
    """
    body = bytes(packet.generate())
    if prefix == 0x31:
        return chr(prefix) + lzf_chunk(body)
    return chr(prefix) + body


def decode(raw, steam_key=None):
    """Decode an inbound ENet payload.

    Returns (packet_id, body, parsed_or_None).

    BattleSpades server always wraps outbound bodies via lzf_compress
    (chunking-only) regardless of prefix. So we always un-chunk; that's what
    shared.lzf.decompress_client handles for both real-LZF and chunking.
    """
    if len(raw) < 2:
        return None, b'', None
    first = raw[0] if isinstance(raw[0], int) else ord(raw[0])
    framed = raw[1:]
    try:
        body = L.decompress_client(framed, MAX_PACKET_DECOMPRESSION_SIZE)
    except Exception:
        # Fallback: treat as raw.
        body = framed
    if steam_key:
        # XOR-decrypt with cyclic key (matches server.connection.decrypt).
        body = b''.join(chr(ord(b) ^ ord(steam_key[i % len(steam_key)]))
                        for i, b in enumerate(body))
    if len(body) < 1:
        return None, b'', None
    pid = ord(body[0])
    cls = _PACKET_BY_ID.get(pid)
    parsed = None
    if cls is not None:
        try:
            inst = cls()
            inst.read(B.ByteReader(body[1:]))
            parsed = inst
        except Exception:
            parsed = None
    return pid, body, parsed


# Packet-id -> class registry. We register every class scenarios might want to
# observe coming back from the server. Disambiguates id=0 collision
# (AddServer, ClockSync, item) by picking ClockSync — gameplay-side meaning.
_INTERESTING = (
    # Handshake / state
    'ClockSync',           # 0
    'WorldUpdate',         # 2
    'ClientData',          # 4
    'SetHP',               # 5
    'ExistingPlayer',      # 14
    'NewPlayerConnection', # 15
    'CreatePlayer',        # 28
    'StateData',           # 45
    'KillAction',          # 46
    'ChatMessage',         # 49
    'SkyboxData',          # 51
    'MapSyncStart',        # 55
    'MapSyncChunk',        # 57
    'MapSyncEnd',          # 59
    'MapDataValidation',   # 60
    'PlayerLeft',          # 64
    'InitialInfo',         # 114
    'PositionData',        # 116

    # Combat / world interactions
    'ShootPacket',         # 6
    'PaintBlockPacket',    # 7
    'UseOrientedItem',     # 10
    'SetColor',            # 11
    'BlockBuild',          # 32
    'BlockBuildColored',   # 33
    'BlockOccupy',         # 34
    'BlockLiberate',       # 35
    'BlockLine',           # 40
    'Damage',              # 37
    'WeaponReload',        # 76
    'ChangeTeam',          # 77
    'ChangeClass',         # 78
    'PlaceMG',             # 87
    'PlaceRocketTurret',   # 88
    'PlaceLandmine',       # 89
    'PlaceMedPack',        # 90
    'PlaceC4',             # 92
    'DetonateC4',          # 93
)

_PACKET_BY_ID = {}
_PACKET_BY_NAME = {}
for _name in _INTERESTING:
    _cls = getattr(P, _name, None)
    if _cls is not None and hasattr(_cls, 'id'):
        _PACKET_BY_ID[_cls.id] = _cls
        _PACKET_BY_NAME[_name] = _cls


def packet_name(pid):
    cls = _PACKET_BY_ID.get(pid)
    return cls.__name__ if cls else 'pid_%d' % pid


# ----- Event log ----------------------------------------------------------

class EventLog(object):
    """Writes JSON-line events to stdout, prefixed with monotonic time since
    bot start. Stderr is reserved for free-form human logs."""

    def __init__(self, sink=None):
        self.t0 = time.time()
        self.sink = sink or sys.stdout

    def emit(self, evt, **fields):
        rec = {'t': round(time.time() - self.t0, 4), 'evt': evt}
        rec.update(fields)
        try:
            line = json.dumps(rec, default=lambda o: '<unrepr {}>'.format(type(o).__name__))
        except Exception as e:
            line = json.dumps({'t': rec['t'], 'evt': 'log_error', 'why': repr(e)})
        self.sink.write(line + '\n')
        self.sink.flush()

    def log(self, *args):
        sys.stderr.write('[bot] ' + ' '.join(str(a) for a in args) + '\n')
        sys.stderr.flush()


# ----- Client -------------------------------------------------------------

class TimeoutError(Exception):
    pass


class Client(object):
    """Single-bot ENet client wrapper.

    Lifecycle:
        c = Client(host='127.0.0.1', port=27015, name='bot0', log=EventLog())
        c.connect()             # blocks until CONNECT event or timeout
        c.expect('InitialInfo') # pumps net until packet seen or timeout
        c.send(packet)
        c.disconnect()
    """

    def __init__(self, host='127.0.0.1', port=27015, name='bot',
                 connect_timeout=5.0, log=None):
        self.host_addr = host
        self.port = int(port)
        self.name = name
        self.connect_timeout = float(connect_timeout)
        self.log = log or EventLog()

        self.host = None
        self.peer = None
        self.connected = False
        self.disconnected = False
        self.steam_key = None
        # received_log: list of (timestamp, packet_id, parsed_or_None)
        self.received_log = []
        # pending_pids: set of packet ids we expect at least once
        self._pending_callbacks = []

        # Captured state from the handshake (filled by do_full_handshake)
        self.last_initial_info = None
        self.last_state_data = None
        self.last_create_player = None
        self.our_player_id = None
        self.spawn_xyz = None

    # --- ENet plumbing -------------------------------------------------

    def connect(self):
        self.log.log('connect to {}:{}'.format(self.host_addr, self.port))
        self.host = enet.Host(None, peerCount=1, channelLimit=1,
                              incomingBandwidth=0, outgoingBandwidth=0)
        self.host.compress_with_range_coder()
        self.peer = self.host.connect(
            enet.Address(self.host_addr.encode('ascii'), self.port),
            1,            # channels
            PROTOCOL_VERSION,  # data byte server's on_connect inspects
        )
        self.log.emit('connecting', host=self.host_addr, port=self.port,
                      protocol_version=PROTOCOL_VERSION)
        # Pump until CONNECT event arrives
        deadline = time.time() + self.connect_timeout
        while not self.connected and not self.disconnected and time.time() < deadline:
            self.pump(0.05)
        if not self.connected:
            raise TimeoutError('connect timed out after {}s'.format(self.connect_timeout))
        return self

    def disconnect(self, drain=1.0):
        if not self.peer:
            return
        self.log.log('disconnect')
        self.peer.disconnect()
        self.log.emit('disconnect_requested')
        end = time.time() + drain
        while not self.disconnected and time.time() < end:
            self.pump(0.05)
        # Clean up the host
        self.host = None
        self.peer = None

    def pump(self, seconds=0.0):
        """Service ENet for at most `seconds` wall-time, dispatching events."""
        if self.host is None:
            return
        deadline = time.time() + max(seconds, 0.0)
        first = True
        while True:
            timeout_ms = 0
            if first or seconds > 0:
                # On the first iteration, allow ENet to wait for an event.
                remaining = deadline - time.time()
                if remaining > 0:
                    timeout_ms = int(remaining * 1000)
            event = self.host.service(timeout_ms)
            first = False
            if event is None or event.type == enet.EVENT_TYPE_NONE:
                if time.time() >= deadline:
                    return
                continue
            self._dispatch(event)
            if time.time() >= deadline:
                return

    def _dispatch(self, event):
        et = event.type
        if et == enet.EVENT_TYPE_CONNECT:
            self.connected = True
            self.log.emit('connected', peer=str(event.peer.address))
        elif et == enet.EVENT_TYPE_DISCONNECT:
            self.disconnected = True
            # event.data is the disconnect reason, if the peer set one
            self.log.emit('disconnected', reason=getattr(event, 'data', None))
        elif et == enet.EVENT_TYPE_RECEIVE:
            data = bytes(event.packet.data)
            self._on_recv(data)

    def _on_recv(self, data):
        pid, body, parsed = decode(data, steam_key=self.steam_key)
        name = packet_name(pid) if pid is not None else 'short_packet'
        rec = {'evt': 'recv', 'name': name, 'id': pid, 'len': len(data)}
        if parsed is not None:
            rec['fields'] = self._summarize_packet(parsed)
        self.received_log.append((time.time(), pid, parsed))
        self.log.emit(**rec)

    def _summarize_packet(self, packet):
        """Produce a JSON-safe dict of a parsed packet's interesting fields.
        Truncate long blobs/lists so the event stream stays readable."""
        out = {}
        for attr in dir(packet):
            if attr.startswith('_') or attr in ('id', 'read', 'write', 'generate',
                                                  'compress_packet'):
                continue
            try:
                v = getattr(packet, attr)
            except Exception:
                continue
            if callable(v):
                continue
            try:
                if isinstance(v, (bytes, bytearray)):
                    if len(v) > 32:
                        out[attr] = '<bytes:%d>' % len(v)
                    else:
                        out[attr] = ''.join('{:02x}'.format(ord(b) if isinstance(b, str) else b)
                                            for b in v)
                elif isinstance(v, (list, tuple)) and len(v) > 6:
                    out[attr] = '<%s:%d>' % (type(v).__name__, len(v))
                elif isinstance(v, str) and len(v) > 80:
                    out[attr] = v[:77] + '...'
                else:
                    json.dumps(v)  # validate serializability
                    out[attr] = v
            except Exception:
                out[attr] = '<unrepr ' + type(v).__name__ + '>'
        return out

    # --- High-level helpers -------------------------------------------

    def send(self, packet, prefix=0x30):
        """Encode + send a shared.packet.<Cls> instance."""
        if not self.connected:
            raise RuntimeError('send before connect')
        wire = encode(packet, prefix=prefix)
        pid = packet.id if hasattr(packet, 'id') else -1
        self.peer.send(0, enet.Packet(wire, enet.PACKET_FLAG_RELIABLE))
        self.log.emit('send', name=type(packet).__name__, id=pid, len=len(wire))

    def expect(self, name, timeout=5.0, after_idx=0):
        """Block until a packet of the given name has been received.
        Returns the parsed packet instance.
        `after_idx` lets you scan only packets received after a known point.
        """
        target_id = _PACKET_BY_NAME.get(name).id if name in _PACKET_BY_NAME else None
        if target_id is None:
            raise ValueError('unknown packet name: ' + name)
        deadline = time.time() + timeout
        # First, look at what we already have
        for ts, pid, parsed in self.received_log[after_idx:]:
            if pid == target_id:
                return parsed
        # Then, pump for more
        while time.time() < deadline:
            n_before = len(self.received_log)
            self.pump(0.1)
            for ts, pid, parsed in self.received_log[n_before:]:
                if pid == target_id:
                    return parsed
            if self.disconnected:
                raise TimeoutError(
                    'disconnected before {} arrived'.format(name))
        raise TimeoutError('timed out waiting for {} after {}s'.format(name, timeout))

    def send_steam_session_ticket(self, ticket=b''):
        """Send SteamSessionTicket(105) with the given ticket bytes.
        An empty ticket is fine for offline / local tests — the server skips
        decryption when steam_key is empty/None."""
        pkt = P.SteamSessionTicket()
        pkt.ticket = ticket
        pkt.ticket_size = len(ticket)
        self.send(pkt)

    def send_clock_sync_reply(self, client_time=0):
        pkt = P.ClockSync()
        pkt.client_time = client_time & 0x7FFFFFFF  # signed int32 safe
        pkt.server_loop_count = 0
        self.send(pkt)

    def send_map_data_validation(self, crc):
        pkt = P.MapDataValidation()
        pkt.crc = crc & 0x7FFFFFFF
        self.send(pkt, prefix=0x31)

    def do_full_handshake(self, team=2, class_id=0, language=0,
                          handshake_timeout=20.0):
        """Run connect → SteamSessionTicket → InitialInfo → MapDataValidation
        → MapSync → StateData → NewPlayerConnection → CreatePlayer + SetHP.

        Captures the relevant packets onto self.{last_initial_info, last_state_data,
        last_create_player, our_player_id, spawn_xyz} for scenarios to use.

        Args mirror NewPlayerConnection fields.
        """
        self.connect()
        self.send_steam_session_ticket(ticket=b'')

        info = self.expect('InitialInfo', timeout=5.0)
        self.last_initial_info = info
        crc = int(getattr(info, 'checksum', 0))
        self.send_map_data_validation(crc=crc)

        self.expect('MapSyncStart', timeout=10.0)
        self.expect('MapSyncEnd', timeout=handshake_timeout)
        state = self.expect('StateData', timeout=5.0)
        self.last_state_data = state

        npc = P.NewPlayerConnection()
        npc.team = team
        npc.class_id = class_id
        npc.local_language = language
        npc.name = self.name
        self.send(npc)

        cp = self.expect('CreatePlayer', timeout=5.0)
        self.last_create_player = cp
        self.our_player_id = int(getattr(cp, 'player_id', -1))
        self.spawn_xyz = (
            float(getattr(cp, 'x', 0.0)),
            float(getattr(cp, 'y', 0.0)),
            float(getattr(cp, 'z', 0.0)),
        )
        self.expect('SetHP', timeout=5.0)
        self.log.emit('handshake_complete',
                       player_id=self.our_player_id,
                       spawn=self.spawn_xyz)
        return self

    def make_client_data(self, loop_count=0, tool_id=2,
                         orientation=(0.0, 1.0, 0.0),
                         up=False, down=False, left=False, right=False,
                         jump=False, crouch=False, sneak=False, sprint=False,
                         primary=False, secondary=False, zoom=False,
                         can_pickup=False, can_display_weapon=False,
                         is_on_fire=False, is_weapon_deployed=False,
                         hover=False, palette_enabled=False,
                         weapon_deployment_yaw=0.0):
        """Build a ClientData(4) packet with sane defaults.
        player_id defaults to our_player_id from the handshake."""
        cd = P.ClientData()
        cd.loop_count = int(loop_count) & 0x7FFFFFFF
        cd.player_id = (self.our_player_id or 0) & 0x7F
        if palette_enabled:
            cd.player_id |= 0x80
        cd.tool_id = int(tool_id) & 0xFF
        cd.o_x, cd.o_y, cd.o_z = orientation
        cd.ooo = 0
        cd.up = bool(up)
        cd.down = bool(down)
        cd.left = bool(left)
        cd.right = bool(right)
        cd.jump = bool(jump)
        cd.crouch = bool(crouch)
        cd.sneak = bool(sneak)
        cd.sprint = bool(sprint)
        cd.primary = bool(primary)
        cd.secondary = bool(secondary)
        cd.zoom = bool(zoom)
        cd.can_pickup = bool(can_pickup)
        cd.can_display_weapon = bool(can_display_weapon)
        cd.is_on_fire = bool(is_on_fire)
        cd.is_weapon_deployed = bool(is_weapon_deployed)
        cd.hover = bool(hover)
        cd.weapon_deployment_yaw = float(weapon_deployment_yaw)
        return cd

    def find_received(self, packet_name, after_idx=0, predicate=None):
        """Scan received_log for a parsed packet matching name (and predicate).
        Returns (timestamp, parsed) or None."""
        target = _PACKET_BY_NAME.get(packet_name)
        if target is None:
            return None
        target_id = target.id
        for ts, pid, parsed in self.received_log[after_idx:]:
            if pid != target_id or parsed is None:
                continue
            if predicate is None or predicate(parsed):
                return (ts, parsed)
        return None

    def wait_for(self, packet_name, predicate=None, timeout=3.0,
                 after_idx=None):
        """Like expect() but with an optional content predicate.
        Returns the parsed packet, or raises TimeoutError.
        after_idx defaults to the current end of received_log."""
        if after_idx is None:
            after_idx = len(self.received_log)
        deadline = time.time() + timeout
        while time.time() < deadline:
            hit = self.find_received(packet_name, after_idx=after_idx,
                                     predicate=predicate)
            if hit is not None:
                return hit[1]
            self.pump(0.1)
            if self.disconnected:
                raise TimeoutError(
                    'disconnected before {} arrived'.format(packet_name))
        raise TimeoutError(
            'timed out after {}s waiting for {} matching predicate'.format(
                timeout, packet_name))
