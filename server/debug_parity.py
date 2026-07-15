"""Server-side localhost parity telemetry transport."""

from __future__ import annotations

import json
import math
import queue
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aoslib import world as native_world

SCHEMA_VERSION = 1
DEFAULT_LOG_DIR = Path('logs')
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 32895
MAX_DATAGRAM_SIZE = 65535
MAP_X = 512
MAP_Y = 512
MAP_Z = 255


@dataclass
class DebugParitySession:
    player_id: int
    player_name: str
    session_id: str
    enabled: bool = False
    last_sample_id: int = 0
    capture_path: Path | None = None
    client_addr: tuple[str, int] | None = None
    next_sample_at: float = 0.0


@dataclass(frozen=True)
class _CaptureWorkItem:
    """One immutable instruction consumed only by the capture writer."""

    kind: str
    path: Path
    session_id: str
    record: dict[str, Any] | None = None
    text: str | None = None


class DebugParityManager:
    """Own opt-in parity transport without putting disk I/O on game threads.

    UDP messages may arrive faster than disk or antivirus scanning can keep
    up. Producers therefore use ``put_nowait`` against a bounded queue. Losing
    diagnostics is preferable to delaying authoritative simulation.
    """

    def __init__(self, server, base_directory: Path | None = None):
        self.server = server
        self.base_directory = Path(base_directory or DEFAULT_LOG_DIR)
        self.base_directory.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[int, DebugParitySession] = {}
        self.latest_summary_path = self.base_directory / 'physics_parity_server_latest.txt'
        self.host = getattr(self.server.config, 'debug_parity_host', DEFAULT_HOST)
        self.port = int(getattr(self.server.config, 'debug_parity_port', DEFAULT_PORT))
        self.socket: socket.socket | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.sample_hz = max(0.1, min(10.0, float(getattr(
            self.server.config, 'debug_parity_sample_hz', 10.0))))
        self.flush_interval = max(0.1, float(getattr(
            self.server.config, 'debug_parity_flush_interval', 1.0)))
        self.flush_batch = max(1, int(getattr(
            self.server.config, 'debug_parity_flush_batch', 128)))
        queue_capacity = max(64, int(getattr(
            self.server.config, 'debug_parity_queue_capacity', 256)))
        self._capture_queue: queue.Queue[object] = queue.Queue(
            maxsize=queue_capacity)
        self._writer_sentinel = object()
        self._writer_thread: threading.Thread | None = None
        self._last_summary_enqueued_at = 0.0
        self._selfrow_next_at: dict[int, float] = {}
        self.dropped_records = 0
        self.rate_limited_samples = 0
        self.writer_errors = 0
        self.override_state = self._reset_native_overrides()
        if (
            getattr(self.server.config, 'debug_parity', False)
            or getattr(self.server.config, 'debug_selfrow', False)
        ):
            self._start_writer()
        if getattr(self.server.config, 'debug_parity', False):
            self._start_transport()

    def _get_native_overrides(self) -> dict[str, float]:
        try:
            overrides = native_world.get_debug_movement_overrides()
        except Exception:
            return {}
        return dict(overrides or {})

    def _reset_native_overrides(self) -> dict[str, float]:
        try:
            native_world.reset_debug_movement_overrides()
        except Exception:
            return {}
        return self._get_native_overrides()

    def _start_transport(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((self.host, self.port))
            sock.settimeout(0.1)
        except OSError:
            self.socket = None
            return
        self.socket = sock
        self._thread = threading.Thread(target=self._recv_loop, name='debug-parity', daemon=True)
        self._thread.start()

    def _start_writer(self) -> None:
        """Start the sole owner of capture file handles."""
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name='debug-parity-writer',
            daemon=True,
        )
        self._writer_thread.start()

    def close(self) -> None:
        """Stop transport, drain accepted telemetry, and close capture files."""
        self._stop_event.set()
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None
        self.override_state = self._reset_native_overrides()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        if self._writer_thread is not None:
            # Shutdown is outside the gameplay hot path, so it may wait for
            # already-accepted diagnostics to drain cleanly.
            self._capture_queue.put(self._writer_sentinel)
            self._writer_thread.join(timeout=5.0)
            self._writer_thread = None

    def on_player_join(self, player) -> None:
        if not getattr(self.server.config, 'debug_parity', False):
            return
        self._ensure_session(player)

    def handle_transport_message(self, message: dict[str, Any], addr: tuple[str, int] | None = None) -> None:
        if not getattr(self.server.config, 'debug_parity', False):
            return
        message_type = str(message.get('message_type') or '')
        if message_type == 'hello':
            self._handle_hello(message, addr)
        elif message_type == 'toggle':
            self._handle_toggle(message, addr)
        elif message_type == 'event':
            self._handle_event(message, addr)
        elif message_type == 'sample':
            self._handle_sample(message, addr)
        elif message_type == 'override_set':
            self._handle_override_set(message, addr)
        elif message_type == 'override_reset':
            self._handle_override_reset(message, addr)
        elif message_type == 'override_reset_all':
            self._handle_override_reset_all(message, addr)

    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.socket is None:
                return
            try:
                raw, addr = self.socket.recvfrom(MAX_DATAGRAM_SIZE)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                message = json.loads(raw.decode('utf-8'))
            except Exception:
                continue
            if isinstance(message, dict):
                self.handle_transport_message(message, addr)

    def _send_message(self, addr: tuple[str, int] | None, payload: dict[str, Any]) -> None:
        if self.socket is None or addr is None:
            return
        try:
            data = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
            self.socket.sendto(data, addr)
        except OSError:
            return

    def _player_from_message(self, message: dict[str, Any]):
        player_id = message.get('player_id')
        if player_id is None:
            player_id = message.get('payload', {}).get('payload', {}).get('snapshot', {}).get('player', {}).get('id')
        try:
            if player_id is not None:
                player_id = int(player_id)
                player = self.server.players.get(player_id)
                if player is not None:
                    return player
        except Exception:
            pass

        player_name = message.get('player_name')
        if player_name is None:
            player_name = message.get('payload', {}).get('payload', {}).get('snapshot', {}).get('player', {}).get('name')
        if player_name:
            return self.server.get_player_by_name(str(player_name))
        return None

    def _requested_session_id(self, message: dict[str, Any]) -> str | None:
        session_id = message.get('session_id')
        if session_id:
            return str(session_id)
        return None

    def _ensure_session(self, player, requested_session_id: str | None = None, addr: tuple[str, int] | None = None) -> DebugParitySession:
        session = self.sessions.get(player.id)
        if session is not None and requested_session_id and session.session_id != requested_session_id:
            session = None
        if session is None:
            session_id = requested_session_id or uuid.uuid4().hex[:12]
            capture_path = self.base_directory / ('physics_parity_server_%s.ndjson' % session_id)
            session = DebugParitySession(player_id=player.id, player_name=player.name, session_id=session_id, capture_path=capture_path)
            self.sessions[player.id] = session
        session.player_name = player.name
        if addr is not None:
            session.client_addr = addr
        return session

    def _hello_payload(self, session: DebugParitySession, enabled: bool = True, reason: str | None = None) -> dict[str, Any]:
        override_names = []
        try:
            override_names = list(native_world.get_debug_movement_override_names())
        except Exception:
            override_names = sorted(self.override_state.keys())
        payload = {
            'message_type': 'hello',
            'schema_version': SCHEMA_VERSION,
            'enabled': 1 if enabled else 0,
            'server_loop_count': int(getattr(self.server, 'loop_count', 0)),
            'session_id': session.session_id,
            'transport_host': self.host,
            'transport_port': self.port,
            'capabilities': {
                'schema_version': SCHEMA_VERSION,
                'transport': 'udp',
                'sample_interval_hz': 10,
                'event_packets': True,
                'diff_packets': True,
                'override_packets': True,
                'tunable_parameters': override_names,
            },
            'override_state': dict(self.override_state),
        }
        if reason:
            payload['reason'] = reason
        return payload

    def _override_state_payload(self, session: DebugParitySession, reason: str | None = None) -> dict[str, Any]:
        payload = {
            'message_type': 'override_state',
            'schema_version': SCHEMA_VERSION,
            'session_id': session.session_id,
            'server_loop_count': int(getattr(self.server, 'loop_count', 0)),
            'payload': {
                'overrides': dict(self.override_state),
            },
        }
        if reason:
            payload['payload']['reason'] = reason
        return payload

    def _handle_hello(self, message: dict[str, Any], addr: tuple[str, int] | None) -> None:
        player = self._player_from_message(message)
        if player is None:
            temp_session = DebugParitySession(player_id=-1, player_name='unknown', session_id=self._requested_session_id(message) or uuid.uuid4().hex[:12])
            self._send_message(addr, self._hello_payload(temp_session, enabled=False, reason='player_not_found'))
            return
        session = self._ensure_session(player, self._requested_session_id(message), addr)
        self._send_message(addr, self._hello_payload(session, enabled=True))
        self._send_message(addr, self._override_state_payload(session, reason='hello'))
        self._write_record(session, {
            'kind': 'hello',
            'timestamp': round(time.time(), 6),
            'player_id': player.id,
            'player_name': player.name,
            'server_loop_count': int(getattr(self.server, 'loop_count', 0)),
        })

    def _handle_toggle(self, message: dict[str, Any], addr: tuple[str, int] | None) -> None:
        player = self._player_from_message(message)
        if player is None:
            return
        session = self._ensure_session(player, self._requested_session_id(message), addr)
        session.enabled = bool(message.get('payload', {}).get('enabled', False))
        self._write_record(session, {
            'kind': 'toggle',
            'timestamp': round(time.time(), 6),
            'player_id': player.id,
            'player_name': player.name,
            'enabled': session.enabled,
            'anchors': message.get('anchors', {}),
            'payload': message.get('payload', {}),
        })
        self._write_latest_summary(session, {'kind': 'toggle', 'enabled': session.enabled, 'player_name': player.name})
        self._send_message(addr, self._hello_payload(session, enabled=True))
        self._send_message(addr, self._override_state_payload(session, reason='toggle'))

    def _handle_event(self, message: dict[str, Any], addr: tuple[str, int] | None) -> None:
        player = self._player_from_message(message)
        if player is None:
            return
        session = self._ensure_session(player, self._requested_session_id(message), addr)
        payload = message.get('payload', {})
        self._write_record(session, {
            'kind': 'event',
            'timestamp': round(time.time(), 6),
            'player_id': player.id,
            'player_name': player.name,
            'event_id': int(payload.get('event_id', 0)),
            'event_name': payload.get('event_name', ''),
            'anchors': message.get('anchors', {}),
            'payload': payload.get('payload', {}),
        })

    def _handle_override_set(self, message: dict[str, Any], addr: tuple[str, int] | None) -> None:
        player = self._player_from_message(message)
        if player is None:
            return
        session = self._ensure_session(player, self._requested_session_id(message), addr)
        payload = message.get('payload', {}) or {}
        name = payload.get('name')
        value = payload.get('value')
        if not name:
            return
        try:
            native_world.set_debug_movement_override(str(name), float(value))
            self.override_state = self._get_native_overrides()
        except Exception as exc:
            self._write_record(session, {
                'kind': 'override_error',
                'timestamp': round(time.time(), 6),
                'player_id': player.id,
                'player_name': player.name,
                'payload': {'name': name, 'value': value, 'error': str(exc)},
            })
            return
        record = {
            'kind': 'override_set',
            'timestamp': round(time.time(), 6),
            'player_id': player.id,
            'player_name': player.name,
            'anchors': message.get('anchors', {}),
            'payload': {'name': str(name), 'value': self.override_state.get(str(name))},
        }
        self._write_record(session, record)
        self._write_latest_summary(session, record)
        self._send_message(addr or session.client_addr, self._override_state_payload(session, reason='override_set'))

    def _handle_override_reset(self, message: dict[str, Any], addr: tuple[str, int] | None) -> None:
        player = self._player_from_message(message)
        if player is None:
            return
        session = self._ensure_session(player, self._requested_session_id(message), addr)
        payload = message.get('payload', {}) or {}
        name = payload.get('name')
        if not name:
            return
        try:
            native_world.reset_debug_movement_override(str(name))
            self.override_state = self._get_native_overrides()
        except Exception as exc:
            self._write_record(session, {
                'kind': 'override_error',
                'timestamp': round(time.time(), 6),
                'player_id': player.id,
                'player_name': player.name,
                'payload': {'name': name, 'error': str(exc)},
            })
            return
        record = {
            'kind': 'override_reset',
            'timestamp': round(time.time(), 6),
            'player_id': player.id,
            'player_name': player.name,
            'anchors': message.get('anchors', {}),
            'payload': {'name': str(name), 'value': self.override_state.get(str(name))},
        }
        self._write_record(session, record)
        self._write_latest_summary(session, record)
        self._send_message(addr or session.client_addr, self._override_state_payload(session, reason='override_reset'))

    def _handle_override_reset_all(self, message: dict[str, Any], addr: tuple[str, int] | None) -> None:
        player = self._player_from_message(message)
        if player is None:
            return
        session = self._ensure_session(player, self._requested_session_id(message), addr)
        self.override_state = self._reset_native_overrides()
        record = {
            'kind': 'override_reset_all',
            'timestamp': round(time.time(), 6),
            'player_id': player.id,
            'player_name': player.name,
            'anchors': message.get('anchors', {}),
            'payload': {'overrides': dict(self.override_state)},
        }
        self._write_record(session, record)
        self._write_latest_summary(session, record)
        self._send_message(addr or session.client_addr, self._override_state_payload(session, reason='override_reset_all'))

    def _handle_sample(self, message: dict[str, Any], addr: tuple[str, int] | None) -> None:
        player = self._player_from_message(message)
        if player is None:
            return
        session = self._ensure_session(player, self._requested_session_id(message), addr)
        now = time.monotonic()
        if now < session.next_sample_at:
            self.rate_limited_samples += 1
            return
        session.next_sample_at = now + (1.0 / self.sample_hz)
        session.last_sample_id = int(message.get('payload', {}).get('sample_id', 0))
        client_payload = message.get('payload', {}).get('payload', {}) or {}
        server_snapshot = self._build_authoritative_snapshot(player)
        diff = self._build_diff(client_payload, server_snapshot)

        response = {
            'message_type': 'diff',
            'schema_version': SCHEMA_VERSION,
            'sample_id': session.last_sample_id,
            'server_loop_count': int(getattr(self.server, 'loop_count', 0)),
            'status': 0 if not diff['flags'] else 1,
            'session_id': session.session_id,
            'payload': {
                'summary': {
                    'health': 'ok' if not diff['flags'] else 'warn',
                    'flag_count': len(diff['flags']),
                    'max_position_delta': diff['position_distance'],
                },
                'diff': diff,
                'server_snapshot': server_snapshot,
                'override_state': dict(self.override_state),
            },
        }
        self._send_message(addr or session.client_addr, response)

        record = {
            'kind': 'sample',
            'timestamp': round(time.time(), 6),
            'player_id': player.id,
            'player_name': player.name,
            'sample_id': session.last_sample_id,
            'anchors': message.get('anchors', {}),
            'client_payload': client_payload,
            'server_snapshot': server_snapshot,
            'diff': diff,
        }
        self._write_record(session, record)
        self._write_latest_summary(session, record)

    def _scan_surface_z(self, x: int, y: int) -> int | None:
        world_manager = getattr(self.server, 'world_manager', None)
        if world_manager is None or getattr(world_manager, 'map', None) is None:
            return None
        if x < 0 or y < 0 or x >= MAP_X or y >= MAP_Y:
            return None
        for z in range(MAP_Z):
            try:
                if world_manager.map.get_solid(x, y, z):
                    return z
            except Exception:
                return None
        return None

    def _player_height(self, player) -> float:
        try:
            return float(player._current_height())
        except Exception:
            return 2.7

    def _player_contact_offset(self, player) -> float:
        try:
            return float(player._current_contact_offset())
        except Exception:
            return 2.25

    def _surface_probe_for_position(self, position: dict[str, Any] | None) -> int | None:
        if not isinstance(position, dict):
            return None
        try:
            x = int(math.floor(float(position.get('x', 0.0))))
            y = int(math.floor(float(position.get('y', 0.0))))
        except Exception:
            return None
        return self._scan_surface_z(x, y)

    def _build_authoritative_snapshot(self, player) -> dict[str, Any]:
        height = self._player_height(player)
        contact_offset = self._player_contact_offset(player)
        surface_z = self._scan_surface_z(int(math.floor(player.x)), int(math.floor(player.y)))
        movement_debug = {}
        try:
            movement_debug = player.get_debug_movement_state()
        except Exception:
            movement_debug = {}
        return {
            'server_loop_count': int(getattr(self.server, 'loop_count', 0)),
            'player_id': player.id,
            'player_name': player.name,
            'team_id': player.team,
            'class_id': player.class_id,
            'tool': player.tool,
            'position': {'x': round(player.x, 4), 'y': round(player.y, 4), 'z': round(player.z, 4)},
            'velocity': {'x': round(player.vx, 4), 'y': round(player.vy, 4), 'z': round(player.vz, 4)},
            'orientation': {'x': round(player.o_x, 4), 'y': round(player.o_y, 4), 'z': round(player.o_z, 4)},
            'side': {'x': round(player.side_x, 4), 'y': round(player.side_y, 4), 'z': round(player.side_z, 4)},
            'head': {'x': round(player.head_x, 4), 'y': round(player.head_y, 4), 'z': round(player.head_z, 4)},
            'eye': {'x': round(player.eye_x, 4), 'y': round(player.eye_y, 4), 'z': round(player.eye_z, 4)},
            'states': {
                'grounded': bool(player.grounded),
                'airborne': bool(player.airborne),
                'wade': bool(player.wade),
                'crouch': bool(player.input.crouch),
                'sprint': bool(player.input.sprint),
                'sneak': bool(player.input.sneak),
                'hover': bool(player.input.hover),
                'jump_held': bool(player.jump_held),
                'pending_jump': bool(player.pending_jump),
            },
            'drift': {
                'magnitude': round(float(getattr(player, 'last_position_drift', 0.0)), 4),
                'vector': tuple(round(float(v), 4) for v in getattr(player, 'last_position_drift_vector', (0.0, 0.0, 0.0))),
                'last_reported_position': getattr(player, 'last_reported_position', None),
            },
            'derived': {
                'height': round(height, 4),
                'contact_offset': round(contact_offset, 4),
                'surface_z': surface_z,
                'anchor_delta': round(surface_z - player.z, 4) if surface_z is not None else None,
                'feet_z': round(player.z + contact_offset, 4),
                'body_bottom_z': round(player.z + height, 4),
            },
            'input_flags': int(player.pack_input_flags()),
            'action_flags': int(player.pack_action_flags()),
            'movement_debug': movement_debug,
            'override_state': dict(self.override_state),
        }

    def _vector_delta(self, client_value: Any, server_value: dict[str, Any]) -> dict[str, float] | None:
        if not isinstance(client_value, dict):
            return None
        deltas = {}
        for axis in ('x', 'y', 'z'):
            if axis not in client_value or axis not in server_value:
                return None
            deltas[axis] = round(float(client_value[axis]) - float(server_value[axis]), 4)
        return deltas

    def _distance(self, delta: dict[str, float] | None) -> float | None:
        if delta is None:
            return None
        return round(math.sqrt(sum(value * value for value in delta.values())), 4)

    def _build_diff(self, client_payload: dict[str, Any], server_snapshot: dict[str, Any]) -> dict[str, Any]:
        snapshot = client_payload.get('snapshot', {})
        client_player = snapshot.get('player', {})
        client_states = client_player.get('states', {})
        client_derived = snapshot.get('derived', {})
        client_state = client_payload.get('client_state', {})
        client_anchors = client_payload.get('anchors', {}) or {}
        server_debug = server_snapshot.get('movement_debug', {}) or {}
        pre_update = server_debug.get('pre_update', {}) or {}
        post_update = server_debug.get('post_update', {}) or {}

        position_delta = self._vector_delta(client_player.get('position'), server_snapshot['position'])
        velocity_delta = self._vector_delta(client_player.get('velocity'), server_snapshot['velocity'])
        orientation_delta = self._vector_delta(client_player.get('orientation'), server_snapshot['orientation'])
        position_distance = self._distance(position_delta)
        velocity_distance = self._distance(velocity_delta)
        orientation_distance = self._distance(orientation_delta)

        client_surface_z = client_derived.get('surface_z')
        server_surface_z = server_snapshot['derived']['surface_z']
        client_grounded = client_derived.get('estimated_grounded')
        server_grounded = server_snapshot['states']['grounded']
        client_airborne = client_states.get('airborne')
        server_airborne = server_snapshot['states']['airborne']
        client_wade = client_states.get('wade')
        server_wade = server_snapshot['states']['wade']
        client_anchor_delta = client_derived.get('delta_anchor_to_surface')
        server_anchor_delta = server_snapshot['derived']['anchor_delta']
        client_step_delta = client_derived.get('step_delta')
        server_step_delta = server_debug.get('step_delta')
        client_landing_speed = client_derived.get('last_landing_speed')
        server_landed = server_debug.get('landed')
        jump_pending_server = server_snapshot['states']['pending_jump']

        categories = {
            'position': bool(position_distance is not None and position_distance > 0.25),
            'velocity': bool(velocity_distance is not None and velocity_distance > 0.25),
            'orientation': bool(orientation_distance is not None and orientation_distance > 0.05),
            'grounded': bool(client_grounded is not None and client_grounded != server_grounded),
            'airborne': bool(client_airborne is not None and client_airborne != server_airborne),
            'wade': bool(client_wade is not None and client_wade != server_wade),
            'jump_edge': bool(client_states.get('jump') is not None and client_states.get('jump') != server_snapshot['states']['jump_held']),
            'crouch_height': bool(client_states.get('crouch') is not None and client_states.get('crouch') != server_snapshot['states']['crouch']),
            'surface_z': bool(
                client_surface_z is not None
                and server_surface_z is not None
                and abs(float(client_surface_z) - float(server_surface_z)) > 0.5
            ),
            'anchor_semantics': bool(
                client_anchor_delta is not None
                and server_anchor_delta is not None
                and abs(float(client_anchor_delta) - float(server_anchor_delta)) > 0.2
            ),
            'step_climb': bool(
                client_step_delta not in (None, 0, 0.0)
                and server_step_delta is not None
                and abs(float(client_step_delta) - float(server_step_delta)) > 0.35
            ),
            'fall_landing': bool(
                (client_landing_speed not in (None, 0, 0.0) and not server_landed)
                or (server_landed and client_landing_speed in (None, 0, 0.0))
            ),
            'smoothing_correction': bool(client_state.get('disable_player_input') or server_snapshot['drift']['magnitude'] > 0.25),
        }
        if client_state.get('disable_player_input'):
            categories['smoothing_correction'] = True

        flags = []
        for name, enabled in categories.items():
            if enabled:
                flags.append('%s_mismatch' % name if name not in ('step_climb', 'fall_landing', 'smoothing_correction') else name)
        if client_state.get('disable_player_input'):
            flags.append('client_input_disabled')
        client_surface_z = client_derived.get('surface_z')

        return {
            'flags': flags,
            'categories': categories,
            'position_delta': position_delta,
            'position_distance': position_distance,
            'velocity_delta': velocity_delta,
            'velocity_distance': velocity_distance,
            'orientation_delta': orientation_delta,
            'orientation_distance': orientation_distance,
            'client_grounded': client_grounded,
            'server_grounded': server_grounded,
            'client_airborne': client_airborne,
            'server_airborne': server_airborne,
            'client_wade': client_wade,
            'server_wade': server_wade,
            'client_surface_z': client_surface_z,
            'server_surface_z': server_surface_z,
            'client_anchor_delta': client_anchor_delta,
            'server_anchor_delta': server_anchor_delta,
            'client_step_delta': client_step_delta,
            'server_step_delta': server_step_delta,
            'client_landing_speed': client_landing_speed,
            'server_landed': server_landed,
            'server_pending_jump': jump_pending_server,
            'client_input_disabled': bool(client_state.get('disable_player_input', False)),
            'client_anchors': client_anchors,
            'server_anchors': {
                'server_loop_count': int(server_snapshot.get('server_loop_count', 0)),
                'pre_update_position': pre_update.get('position'),
                'post_update_position': post_update.get('position'),
            },
        }

    def _write_record(self, session: DebugParitySession, record: dict[str, Any]) -> None:
        if session.capture_path is None:
            return
        self._enqueue_capture(_CaptureWorkItem(
            kind='record',
            path=session.capture_path,
            session_id=session.session_id,
            record=record,
        ))

    def write_selfrow_sample(self, player, stamp: int) -> None:
        """Queue one local WorldUpdate reconciliation sample if enabled.

        Called from the gameplay tick after a self-row is serialized.  The
        method never opens, writes, flushes, or waits on files from that tick;
        accepted samples go through the bounded writer queue and overflow is
        counted as dropped diagnostics.
        """
        if not getattr(self.server.config, 'debug_selfrow', False):
            return
        if self._writer_thread is None:
            return

        player_id = int(getattr(player, 'id', -1))
        now = time.monotonic()
        if now < self._selfrow_next_at.get(player_id, 0.0):
            self.rate_limited_samples += 1
            return
        sample_hz = max(0.1, min(10.0, float(getattr(
            self.server.config, 'debug_parity_sample_hz', 10.0))))
        self._selfrow_next_at[player_id] = now + (1.0 / sample_hz)

        record = {
            'kind': 'selfrow',
            'timestamp': round(time.time(), 6),
            'server_tick': int(getattr(self.server, 'loop_count', 0)),
            'stamp': int(stamp),
            'input_loop': int(getattr(player, 'last_applied_input_loop', 0) or 0),
            'player_id': player_id,
            'player_name': str(getattr(player, 'name', '')),
            'x': round(float(getattr(player, 'x', 0.0)), 5),
            'y': round(float(getattr(player, 'y', 0.0)), 5),
            'z': round(float(getattr(player, 'z', 0.0)), 5),
        }
        self._enqueue_capture(_CaptureWorkItem(
            kind='record',
            path=self.base_directory / 'selfrow_samples.ndjson',
            session_id='selfrow',
            record=record,
        ))

    def _write_latest_summary(self, session: DebugParitySession, record: dict[str, Any]) -> None:
        lines = [
            'session_id=%s' % session.session_id,
            'player=%s (%s)' % (session.player_name, session.player_id),
            'kind=%s' % record.get('kind'),
            'transport=%s:%s' % (self.host, self.port),
        ]
        if record.get('kind') == 'sample':
            diff = record.get('diff', {})
            lines.extend([
                'sample_id=%s' % record.get('sample_id'),
                'flags=%s' % (','.join(diff.get('flags', [])) or '-'),
                'categories=%s' % (
                    ','.join(
                        key for key, enabled in (diff.get('categories', {}) or {}).items() if enabled
                    ) or '-'
                ),
                'position_distance=%s' % diff.get('position_distance'),
                'velocity_distance=%s' % diff.get('velocity_distance'),
                'orientation_distance=%s' % diff.get('orientation_distance'),
            ])
        elif record.get('kind') == 'toggle':
            lines.append('enabled=%s' % record.get('enabled'))
        elif record.get('kind', '').startswith('override_'):
            lines.append('override_payload=%s' % record.get('payload'))
        if self.override_state:
            lines.append('overrides=%s' % self.override_state)
        now = time.monotonic()
        if now - self._last_summary_enqueued_at < 1.0:
            return
        self._last_summary_enqueued_at = now
        self._enqueue_capture(_CaptureWorkItem(
            kind='summary',
            path=self.latest_summary_path,
            session_id=session.session_id,
            text='\n'.join(lines) + '\n',
        ))

    def _enqueue_capture(self, item: _CaptureWorkItem) -> None:
        """Accept telemetry without ever waiting for the writer thread."""
        if self._writer_thread is None:
            return
        try:
            self._capture_queue.put_nowait(item)
        except queue.Full:
            self.dropped_records += 1

    def _writer_loop(self) -> None:
        """Serialize and batch file writes away from simulation/UDP handling."""
        handles: dict[Path, Any] = {}
        pending_records = 0
        last_flush = time.monotonic()

        def flush_handles() -> None:
            nonlocal pending_records, last_flush
            for handle in handles.values():
                try:
                    handle.flush()
                except OSError:
                    self.writer_errors += 1
            pending_records = 0
            last_flush = time.monotonic()

        try:
            while True:
                timeout = max(0.01, self.flush_interval - (
                    time.monotonic() - last_flush))
                try:
                    item = self._capture_queue.get(timeout=timeout)
                except queue.Empty:
                    flush_handles()
                    continue

                try:
                    if item is self._writer_sentinel:
                        break
                    if not isinstance(item, _CaptureWorkItem):
                        continue
                    if item.kind == 'record' and item.record is not None:
                        handle = handles.get(item.path)
                        if handle is None:
                            handle = item.path.open('a', encoding='utf-8')
                            handles[item.path] = handle
                        payload = dict(item.record)
                        payload['session_id'] = item.session_id
                        handle.write(json.dumps(payload, sort_keys=True))
                        handle.write('\n')
                        pending_records += 1
                    elif item.kind == 'summary' and item.text is not None:
                        item.path.write_text(item.text, encoding='utf-8')
                except (OSError, TypeError, ValueError):
                    # Diagnostics are best effort. A malformed record or full
                    # disk must not kill the server or its writer permanently.
                    self.writer_errors += 1
                finally:
                    self._capture_queue.task_done()

                if (pending_records >= self.flush_batch
                        or time.monotonic() - last_flush >= self.flush_interval):
                    flush_handles()
        finally:
            flush_handles()
            for handle in handles.values():
                try:
                    handle.close()
                except OSError:
                    self.writer_errors += 1
