import json
from pathlib import Path
from types import SimpleNamespace

from server.config import ServerConfig
from server.debug_parity import DebugParityManager
from server.world_manager import WorldManager


class DummyPlayer:
    def __init__(self, world_manager):
        self.id = 4
        self.name = 'KikoTs'
        self.team = 2
        self.class_id = 0
        self.tool = 7
        self.x = 100.5
        self.y = 100.5
        self.z = 59.3
        self.vx = 0.25
        self.vy = 0.5
        self.vz = -0.1
        self.o_x = 1.0
        self.o_y = 0.0
        self.o_z = 0.0
        self.side_x = 0.0
        self.side_y = 1.0
        self.side_z = 0.0
        self.head_x = 0.0
        self.head_y = 0.0
        self.head_z = 1.0
        self.eye_x = self.x
        self.eye_y = self.y
        self.eye_z = self.z
        self.grounded = True
        self.airborne = False
        self.wade = False
        self.jump_held = False
        self.pending_jump = False
        self.input = SimpleNamespace(crouch=False, sprint=False, sneak=False, hover=False)
        self.last_position_drift = 0.0
        self.last_position_drift_vector = (0.0, 0.0, 0.0)
        self.last_reported_position = (self.x, self.y, self.z)
        self.sent_packets = []
        self.world_manager = world_manager

    def _current_height(self):
        return 2.7

    def _current_contact_offset(self):
        return 2.25

    def pack_input_flags(self):
        return 0

    def pack_action_flags(self):
        return 0

    def send_packet(self, packet):
        self.sent_packets.append(packet)

    def get_debug_movement_state(self):
        return {
            'pre_update': {
                'position': (self.x, self.y, self.z),
                'velocity': (self.vx, self.vy, self.vz),
                'grounded': self.grounded,
            },
            'post_update': {
                'position': (self.x, self.y, self.z),
                'velocity': (self.vx, self.vy, self.vz),
                'grounded': self.grounded,
            },
            'collision_count': 0,
            'collision_preview': [],
            'trigger_jump': False,
            'buffered_jump_active': False,
            'buffered_jump_remaining': 0.0,
            'landed': False,
            'step_delta': 0.0,
            'fall_result': 0,
            'native_result': 0,
            'dt': 0.016667,
        }


def _make_server(tmp_path):
    config = ServerConfig(debug_parity=True)
    world_manager = WorldManager(config)
    world_manager.generate_flat_map()
    server = SimpleNamespace(config=config, loop_count=321, world_manager=world_manager, players={})
    server.get_player_by_name = lambda name: next((player for player in server.players.values() if player.name.lower().startswith(name.lower())), None)
    manager = DebugParityManager(server, base_directory=tmp_path)
    server.debug_parity = manager
    return server, manager


def test_debug_parity_hello_replies_without_game_packet(tmp_path):
    server, manager = _make_server(tmp_path)
    player = DummyPlayer(server.world_manager)
    server.players[player.id] = player
    replies = []
    manager._send_message = lambda addr, payload: replies.append(payload)

    manager.handle_transport_message({'message_type': 'hello', 'session_id': 'abc123', 'player_id': player.id, 'player_name': player.name}, ('127.0.0.1', 40000))

    assert replies
    hello = next(payload for payload in replies if payload.get('message_type') == 'hello')
    assert hello['message_type'] == 'hello'
    assert hello['enabled'] == 1
    assert hello['session_id'] == 'abc123'
    assert player.sent_packets == []
    manager.close()


def test_debug_parity_sample_generates_diff_and_logs(tmp_path):
    server, manager = _make_server(tmp_path)
    player = DummyPlayer(server.world_manager)
    server.players[player.id] = player
    replies = []
    manager._send_message = lambda addr, payload: replies.append(payload)

    manager.handle_transport_message({'message_type': 'hello', 'session_id': 'abc123', 'player_id': player.id, 'player_name': player.name}, ('127.0.0.1', 40000))
    manager.handle_transport_message({'message_type': 'toggle', 'session_id': 'abc123', 'player_id': player.id, 'player_name': player.name, 'payload': {'enabled': True}}, ('127.0.0.1', 40000))
    manager.handle_transport_message(
        {
            'message_type': 'sample',
            'session_id': 'abc123',
            'player_id': player.id,
            'player_name': player.name,
            'anchors': {'client_loop_count': 120, 'world_update_loop_count': 119, 'client_time': 1111, 'server_loop_count': 320},
            'payload': {
                'sample_id': 9,
                'payload': {
                    'snapshot': {
                        'player': {
                            'id': player.id,
                            'name': player.name,
                            'position': {'x': 100.5, 'y': 100.5, 'z': 59.3},
                            'velocity': {'x': 0.25, 'y': 0.5, 'z': -0.1},
                            'orientation': {'x': 1.0, 'y': 0.0, 'z': 0.0},
                            'states': {'crouch': False, 'jump': False},
                        },
                        'derived': {'estimated_grounded': True, 'surface_z': 62, 'delta_anchor_to_surface': 2.7},
                    },
                    'client_state': {'disable_player_input': False},
                },
            },
        },
        ('127.0.0.1', 40000),
    )

    diff_payload = replies[-1]
    assert diff_payload['message_type'] == 'diff'
    assert diff_payload['sample_id'] == 9
    assert diff_payload['payload']['diff']['flags'] == []

    # close() drains the bounded writer queue; callers never need to wait for
    # capture I/O while the server is running.
    manager.close()
    capture_files = list(Path(tmp_path).glob('physics_parity_server_*.ndjson'))
    assert capture_files
    text = capture_files[0].read_text(encoding='utf-8')
    assert '"kind": "sample"' in text


def test_debug_parity_override_round_trip(tmp_path):
    server, manager = _make_server(tmp_path)
    player = DummyPlayer(server.world_manager)
    server.players[player.id] = player
    replies = []
    manager._send_message = lambda addr, payload: replies.append(payload)

    manager.handle_transport_message({'message_type': 'hello', 'session_id': 'abc123', 'player_id': player.id, 'player_name': player.name}, ('127.0.0.1', 40000))
    manager.handle_transport_message({'message_type': 'toggle', 'session_id': 'abc123', 'player_id': player.id, 'player_name': player.name, 'payload': {'enabled': True}}, ('127.0.0.1', 40000))
    manager.handle_transport_message(
        {
            'message_type': 'override_set',
            'session_id': 'abc123',
            'player_id': player.id,
            'player_name': player.name,
            'payload': {'name': 'standing_pos_above_ground', 'value': 2.4},
        },
        ('127.0.0.1', 40000),
    )

    assert replies[-1]['message_type'] == 'override_state'
    assert replies[-1]['payload']['overrides']['standing_pos_above_ground'] == 2.4
    assert replies[-1]['payload']['overrides']['standing_height'] == 2.85

    manager.handle_transport_message(
        {
            'message_type': 'override_reset',
            'session_id': 'abc123',
            'player_id': player.id,
            'player_name': player.name,
            'payload': {'name': 'standing_pos_above_ground'},
        },
        ('127.0.0.1', 40000),
    )

    assert replies[-1]['message_type'] == 'override_state'
    assert replies[-1]['payload']['overrides']['standing_pos_above_ground'] == 2.25
    assert replies[-1]['payload']['overrides']['standing_height'] == 2.7
    manager.close()


def test_debug_parity_reports_wade_mismatch(tmp_path):
    server, manager = _make_server(tmp_path)
    player = DummyPlayer(server.world_manager)
    player.wade = True
    server.players[player.id] = player
    replies = []
    manager._send_message = lambda addr, payload: replies.append(payload)

    manager.handle_transport_message(
        {
            'message_type': 'hello',
            'session_id': 'abc123',
            'player_id': player.id,
            'player_name': player.name,
        },
        ('127.0.0.1', 40000),
    )
    manager.handle_transport_message(
        {
            'message_type': 'toggle',
            'session_id': 'abc123',
            'player_id': player.id,
            'player_name': player.name,
            'payload': {'enabled': True},
        },
        ('127.0.0.1', 40000),
    )
    manager.handle_transport_message(
        {
            'message_type': 'sample',
            'session_id': 'abc123',
            'player_id': player.id,
            'player_name': player.name,
            'anchors': {
                'client_loop_count': 120,
                'world_update_loop_count': 119,
                'client_time': 1111,
                'server_loop_count': 320,
            },
            'payload': {
                'sample_id': 11,
                'payload': {
                    'snapshot': {
                        'player': {
                            'id': player.id,
                            'name': player.name,
                            'position': {'x': 100.5, 'y': 100.5, 'z': 59.3},
                            'velocity': {'x': 0.25, 'y': 0.5, 'z': -0.1},
                            'orientation': {'x': 1.0, 'y': 0.0, 'z': 0.0},
                            'states': {'crouch': False, 'jump': False, 'wade': False},
                        },
                        'derived': {'estimated_grounded': True, 'surface_z': 62, 'delta_anchor_to_surface': 2.7},
                    },
                    'client_state': {'disable_player_input': False},
                },
            },
        },
        ('127.0.0.1', 40000),
    )

    diff_payload = replies[-1]
    assert diff_payload['message_type'] == 'diff'
    assert 'wade_mismatch' in diff_payload['payload']['diff']['flags']
    assert diff_payload['payload']['diff']['categories']['wade'] is True
    manager.close()


def test_debug_parity_rate_limits_before_snapshot_work(tmp_path):
    server, manager = _make_server(tmp_path)
    player = DummyPlayer(server.world_manager)
    server.players[player.id] = player
    replies = []
    snapshot_calls = []
    manager._send_message = lambda addr, payload: replies.append(payload)
    manager._build_authoritative_snapshot = lambda current: (
        snapshot_calls.append(current) or {'snapshot': True}
    )
    manager._build_diff = lambda client, server: {
        'flags': [],
        'position_distance': 0.0,
    }
    message = {
        'message_type': 'sample',
        'session_id': 'rate-limit',
        'player_id': player.id,
        'payload': {'sample_id': 1, 'payload': {}},
    }

    manager.handle_transport_message(message, ('127.0.0.1', 40000))
    message['payload']['sample_id'] = 2
    manager.handle_transport_message(message, ('127.0.0.1', 40000))

    assert len(snapshot_calls) == 1
    assert len(replies) == 1
    assert manager.rate_limited_samples == 1
    manager.close()
