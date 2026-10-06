"""Client-input validation for terrain, combat, and projectile packets.

Each test drives the authoritative server path a retail packet reaches and
checks that a forged/out-of-range request is rejected (or clamped) while the
legitimate stock-client case keeps working.
"""

import asyncio
import math
import time

import pytest

import shared.constants as C
from protocol.packet_handler import PacketHandler
from server.audio import play_sound, play_sound_to
from server.combat_runtime import BUILD_REACH, CombatSystem
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.oriented_actions import (
    OrientedActionService,
    max_fuse,
    max_launch_speed,
)
from server.player import Player
from shared.packet import (
    BlockBuild,
    BlockBuildColored,
    BlockLine,
    BlockSuckerPacket,
    ErasePrefabAction,
    PaintBlockPacket,
    ShootFeedbackPacket,
    ShootPacket,
)


class _Connection:
    def __init__(self, server):
        self.server = server
        self.player = None
        self.in_game = True
        self.sent = []
        self.reserved_player_id = None

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append((data, reliable))

    def on_disconnect(self):
        pass


def _server_player(tool, loadout=None, *, player_id=0, team=TEAM1,
                   position=(100.5, 100.5, 59.75), server=None):
    if server is None:
        server = BattleSpadesServer(ServerConfig())
        server.world_manager.generate_flat_map()
    connection = _Connection(server)
    player = Player(player_id, f"Validation{player_id}", team, C.RIFLE_TOOL, connection)
    connection.player = player
    player.class_id = {
        int(C.BLOCK_SUCKER_TOOL): int(C.CLASS_MINER),
    }.get(int(tool), int(C.CLASS_SOLDIER))
    player.loadout = list(loadout if loadout is not None else [tool])
    player.spawn(*position)
    player.set_tool(tool, raw=True)
    server.players[player.id] = player
    server.connections[player.id] = connection
    server.teams[team].add_player(player)
    return server, player, connection


def _events(server, name):
    return [args for event, args in server._mode_events if event == name]


def _handle(server, player, packet):
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))


def _dig_packet(player, direction=(0.0, 0.0, 1.0)):
    packet = ShootPacket()
    packet.loop_count = 1
    packet.shooter_id = player.id
    packet.shot_on_world_update = 0
    packet.x, packet.y, packet.z = player.eye
    packet.ori_x, packet.ori_y, packet.ori_z = direction
    packet.damage = 0.0
    packet.penetration = 0
    packet.affect_shooter = 0
    packet.secondary = 0
    packet.seed = 0
    return packet


# --- 1. melee digging reaches the mode ------------------------------------

@pytest.mark.parametrize("tool", [C.SPADE_TOOL, C.PICKAXE_TOOL])
def test_shootpacket_dig_queues_mined_blocks_destroyed_once(tool):
    server, player, _ = _server_player(tool)
    player.orientation = (0.0, 0.0, 1.0)
    ground = (100, 100, 62)
    assert server.world_manager.get_solid(*ground)
    server._mode_events.clear()

    _handle(server, player, _dig_packet(player))

    assert not server.world_manager.get_solid(*ground)
    events = _events(server, "on_blocks_destroyed")
    assert len(events) == 1
    actor, cells, mined = events[0]
    assert actor is player
    assert mined is True
    assert ground in cells
    assert all(not server.world_manager.get_solid(*cell) for cell in cells)


def test_machete_dig_queues_event_only_when_a_cell_breaks(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("server.combat_runtime.time.monotonic", lambda: clock[0])
    server, player, _ = _server_player(C.MACHETE_TOOL)
    player.orientation = (0.0, 0.0, 1.0)
    server._mode_events.clear()
    swings = 0
    while server.world_manager.get_solid(100, 100, 62) and swings < 10:
        server.combat.handle_shot(player, _dig_packet(player))
        clock[0] += player.get_weapon_profile().fire_interval
        swings += 1
    events = _events(server, "on_blocks_destroyed")
    assert not server.world_manager.get_solid(100, 100, 62)
    assert len(events) == 1
    assert events[0][2] is True


# --- 2. BlockLiberate(35) -------------------------------------------------

def test_block_tool_liberate_requires_reach_and_cadence(monkeypatch):
    import server.combat_runtime as combat_runtime

    clock = [1000.0]
    monkeypatch.setattr(combat_runtime.time, "monotonic", lambda: clock[0])
    server, player, _ = _server_player(C.BLOCK_TOOL)
    server.world_mutations = None
    far = (300, 300, 62)
    near = (102, 100, 62)
    near2 = (103, 100, 62)
    packet = type("P", (), {})()
    packet.loop_count = 1

    packet.x, packet.y, packet.z = far
    assert server.combat.handle_block_destroy(player, packet) is False
    assert server.world_manager.get_solid(*far)

    packet.x, packet.y, packet.z = near
    assert server.combat.handle_block_destroy(player, packet) is True
    assert not server.world_manager.get_solid(*near)

    calls = []
    server.world_manager.destroy_blocks = lambda cells: calls.append(cells) or []
    packet.x, packet.y, packet.z = near2
    server.world_manager.set_block(*near2, True, (1, 2, 3))
    assert server.combat.handle_block_destroy(player, packet) is False
    assert calls == []

    clock[0] += 0.25
    assert server.combat.handle_block_destroy(player, packet) is True
    assert calls == [[near2]]


def test_spade_liberate_requires_reach():
    server, player, _ = _server_player(C.SPADE_TOOL)
    packet = type("P", (), {})()
    packet.loop_count = 1
    packet.x, packet.y, packet.z = (200, 200, 62)
    assert server.combat.handle_block_destroy(player, packet) is False
    assert server.world_manager.get_solid(200, 200, 62)


# --- 3. ErasePrefabAction(31) ---------------------------------------------

def test_erase_prefab_is_inert_outside_ugc():
    server, player, _ = _server_player(C.PREFAB_TOOL)
    assert not bool(getattr(server.config, "ugc_runtime", False))
    called = []
    server.world_manager.destroy_blocks = lambda cells: called.append(cells) or []
    packet = ErasePrefabAction()
    packet.loop_count = 1
    packet.prefab_name = "anything"
    packet.position = (100.0, 100.0, 62.0)
    packet.prefab_yaw = packet.prefab_pitch = packet.prefab_roll = 0
    _handle(server, player, packet)
    assert called == []


# --- 4. BlockLine(40) -----------------------------------------------------

def _line(player, start, end):
    packet = BlockLine()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x1, packet.y1, packet.z1 = start
    packet.x2, packet.y2, packet.z2 = end
    return packet


def test_block_line_rejects_huge_span_before_expansion(monkeypatch):
    server, player, _ = _server_player(C.BLOCK_TOOL)
    blocks = player.blocks

    def explode(*_args):
        raise AssertionError("line expanded before the span check")

    monkeypatch.setattr(server.combat, "block_line_cells", explode)
    start = time.perf_counter()
    assert server.combat.handle_block_line(
        player, _line(player, (0, 0, 0), (511, 511, 238))
    ) is False
    assert server.combat.handle_block_line(
        player, _line(player, (-32768, 5, 5), (32767, 5, 5))
    ) is False
    assert time.perf_counter() - start < 0.05
    assert player.blocks == blocks


def test_block_line_rejects_out_of_reach_endpoints():
    server, player, _ = _server_player(C.BLOCK_TOOL)
    blocks = player.blocks
    assert server.combat.handle_block_line(
        player, _line(player, (150, 150, 61), (152, 150, 61))
    ) is False
    assert player.blocks == blocks


def test_block_line_in_reach_is_accepted():
    server, player, _ = _server_player(C.BLOCK_TOOL)
    server.world_mutations = None
    blocks = player.blocks
    assert server.combat.handle_block_line(
        player, _line(player, (103, 100, 61), (105, 100, 61))
    ) is True
    assert player.blocks == blocks - 3
    assert server.world_manager.get_solid(104, 100, 61)


def test_block_line_commit_echoes_only_committed_cells():
    server, player, _ = _server_player(C.BLOCK_TOOL)
    _obs_server, observer, observer_conn = _server_player(
        C.RIFLE_TOOL, player_id=1, team=TEAM2, position=(110.5, 110.5, 59.75),
        server=server,
    )
    cells = ((103, 100, 61), (104, 100, 61), (105, 100, 61))
    rejected = cells[1]
    original = server.world_manager.set_block

    def set_block(x, y, z, *args, **kwargs):
        if (x, y, z) == rejected:
            return False
        return original(x, y, z, *args, **kwargs)

    server.world_manager.set_block = set_block
    player.blocks -= len(cells)
    blocks = player.blocks
    journal_before = len(getattr(server, "_map_mutation_journal", []) or [])
    observer_conn.sent.clear()
    server.combat._commit_block_line(
        player, 1, (*cells[0], *cells[-1]), cells, 0x112233
    )
    echoed = []
    for data, _reliable in observer_conn.sent:
        if data[0] == BlockBuildColored.id:
            from shared.bytes import ByteReader
            parsed = BlockBuildColored(ByteReader(data[1:]))
            echoed.append((parsed.x, parsed.y, parsed.z))
    assert rejected not in echoed
    assert sorted(echoed) == sorted([cells[0], cells[2]])
    assert player.blocks == blocks + 1
    del journal_before


# --- 5. BlockBuild(32) / body overlap / prefab reach ----------------------

def test_block_build_rejects_far_cell():
    server, player, _ = _server_player(C.BLOCK_TOOL)
    blocks = player.blocks
    packet = BlockBuild()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x, packet.y, packet.z = (140, 100, 61)
    packet.block_type = 0
    assert server.combat.handle_block_build(player, packet) is False
    assert player.blocks == blocks


def test_human_build_inside_another_player_is_refunded():
    server, builder, _ = _server_player(C.BLOCK_TOOL)
    _server_player(
        C.RIFLE_TOOL, player_id=1, team=TEAM2, position=(103.5, 100.5, 59.75),
        server=server,
    )
    cell = (103, 100, 61)
    builder.blocks -= 1
    blocks = builder.blocks
    server.combat._commit_block_build(builder, 1, cell, 0x445566)
    assert not server.world_manager.get_solid(*cell)
    assert builder.blocks == blocks + 1


def test_human_build_beside_other_player_still_commits():
    server, builder, _ = _server_player(C.BLOCK_TOOL)
    _server_player(
        C.RIFLE_TOOL, player_id=1, team=TEAM2, position=(106.5, 100.5, 59.75),
        server=server,
    )
    cell = (103, 100, 61)
    builder.blocks -= 1
    server.combat._commit_block_build(builder, 1, cell, 0x445566)
    assert server.world_manager.get_solid(*cell)


@pytest.mark.parametrize("crouched,commits", ((False, True), (True, False)))
def test_what_a_builder_gets_flooring_the_column_its_own_body_straddles(crouched, commits):
    """Pins today's answer; it is not established that retail gives the same.

    The refused body span is floor(z)..floor(z + 2) whatever the stance.
    Standing, that ends one cell above the floor the builder stands on.
    Crouched, the eye is 0.9 lower and the same span takes in the floor
    layer, although the collision box ends at the feet, 1.35 below the eye.
    Retail decides this in GameScene.can_place_block_on_player, which is
    compiled and has not been read.
    """
    floor = 62
    above_ground = (C.PLAYER_CROUCHING_POS_ABOVE_GROUND if crouched
                    else C.PLAYER_STANDING_POS_ABOVE_GROUND)
    server, builder, _ = _server_player(
        C.BLOCK_TOOL, position=(103.7, 100.5, floor - above_ground))
    builder.input.crouch = crouched
    cell = (104, 100, floor)
    server.world_manager.set_block(*cell, False, 0)  # a gap in the floor, half under the body
    builder.blocks -= 1
    blocks = builder.blocks
    server.combat._commit_block_build(builder, 1, cell, 0x445566)
    assert bool(server.world_manager.get_solid(*cell)) is commits
    assert builder.blocks == blocks + (0 if commits else 1)


def test_prefab_reach_helper():
    from server.prefab_actions import PrefabActionService

    player = type("P", (), {"eye": (100.5, 100.5, 59.75)})()
    near = [((104, 100, 61), (0, 0, 0))]
    far = [((100 + int(BUILD_REACH) + 5, 100, 61), (0, 0, 0))]
    assert PrefabActionService._within_build_reach(player, near) is True
    assert PrefabActionService._within_build_reach(player, far) is False
    assert PrefabActionService._within_build_reach(player, far + near) is True


# --- 6. PaintBlock(7) -----------------------------------------------------

def _paint(position, color=(1, 2, 3)):
    packet = PaintBlockPacket()
    packet.loop_count = 1
    packet.x, packet.y, packet.z = position
    packet.color = color
    return packet


def test_paint_requires_reach():
    server, player, _ = _server_player(C.BLOCK_TOOL)
    assert server.combat.handle_paint_packet(player, _paint((300, 300, 62))) is False
    assert server.combat.handle_paint_packet(player, _paint((102, 100, 62))) is True


def test_paint_is_rate_limited():
    server, player, _ = _server_player(C.BLOCK_TOOL)
    accepted = 0
    for index in range(40):
        color = (index + 10, 20, 30)
        if server.combat.handle_paint_packet(player, _paint((102, 100, 62), color)):
            accepted += 1
    assert 1 <= accepted < 40


# --- 7. UseOrientedItem(10) -----------------------------------------------

def test_oriented_launch_rejects_far_origin_and_clamps_speed_and_fuse():
    player = type("P", (), {"eye": (100.5, 100.5, 59.75)})()
    tool = int(C.GRENADE_TOOL)
    validate = OrientedActionService._validated_launch

    assert validate(player, tool, (300.0, 300.0, 50.0), (1.0, 0.0, 0.0), 2.0) is None
    assert validate(player, tool, (math.nan, 0.0, 0.0), (1.0, 0.0, 0.0), 2.0) is None
    assert validate(player, tool, player.eye, (1.0, 0.0, 0.0), math.inf) is None

    _pos, velocity, fuse = validate(player, tool, player.eye, (1e5, 0.0, 0.0), 99.0)
    assert math.isclose(math.hypot(*velocity), max_launch_speed(tool))
    assert fuse == max_fuse(tool) == float(C.GRENADE_EXPLOSION_FUSE)

    _pos, velocity, fuse = validate(player, tool, player.eye, (40.0, 0.0, 0.0), -5.0)
    assert velocity == (40.0, 0.0, 0.0)
    assert fuse == 0.0


def test_oriented_use_spawns_only_validated_launch():
    server, player, _ = _server_player(C.GRENADE_TOOL, [C.GRENADE_TOOL])
    service = OrientedActionService(server)
    spawned = []
    server.spawn_grenade = lambda _player, packet: spawned.append(packet) or True

    assert service.use(
        player, tool_id=int(C.GRENADE_TOOL), position=(400.0, 400.0, 10.0),
        velocity=(10.0, 0.0, 0.0), fuse=2.0,
    ) is False
    assert spawned == []

    assert service.use(
        player, tool_id=int(C.GRENADE_TOOL), position=player.eye,
        velocity=(1e6, 0.0, 0.0), fuse=50.0,
    ) is True
    packet = spawned[-1]
    assert math.hypot(*packet.velocity) <= max_launch_speed(C.GRENADE_TOOL) + 1e-3
    assert packet.value <= max_fuse(C.GRENADE_TOOL) + 1e-6


# --- 8. ShootPacket(6) non-finite values ----------------------------------

@pytest.mark.parametrize("field,value", [
    ("x", math.nan), ("y", math.inf), ("ori_x", math.nan), ("ori_z", -math.inf),
])
def test_shot_with_non_finite_values_is_rejected(field, value):
    server, player, _ = _server_player(C.RIFLE_TOOL)
    player.orientation = (1.0, 0.0, 0.0)
    packet = _dig_packet(player, direction=(1.0, 0.0, 0.0))
    packet_values = {
        "x": packet.x, "y": packet.y, "z": packet.z,
        "ori_x": packet.ori_x, "ori_y": packet.ori_y, "ori_z": packet.ori_z,
    }
    packet_values[field] = value
    fake = type("P", (), packet_values)()
    assert server.combat._validate_shot_packet(player, fake) is None


# --- 9. BlockSucker(94) relay ---------------------------------------------

def test_block_sucker_relays_only_state_changes_and_accepted_shots():
    server, player, connection = _server_player(C.BLOCK_SUCKER_TOOL)
    server.world_manager.raycast = lambda *_args: None

    def relays():
        return [data for data, _ in connection.sent if data[0] == BlockSuckerPacket.id]

    packet = BlockSuckerPacket()
    packet.loop_count = 1
    packet.shooter_id = player.id
    packet.state = int(C.BLOCK_SUCKER_STATE_WARMING_UP)
    packet.shot = 0
    for _ in range(5):
        _handle(server, player, packet)
    assert len(relays()) == 1
    # Retail spin-up: full power only after BLOCK_SUCKER_WARM_UP_DELAY.
    life, started, loop = player._block_sucker_warm
    player._block_sucker_warm = (life, started - 2.0, loop)

    packet.state = int(C.BLOCK_SUCKER_STATE_FULL_POWER)
    packet.shot = 1
    for _ in range(5):
        _handle(server, player, packet)
    # One state change carrying the first accepted shot; the rest are inside
    # the shoot interval.
    assert len(relays()) == 2


# --- 10. cosmetic traffic is unreliable -----------------------------------

class _Recorder:
    def __init__(self):
        self.calls = []

    def broadcast(self, data, **kwargs):
        self.calls.append((data, kwargs))

    def send(self, data, reliable=True):
        self.calls.append((data, {"reliable": reliable}))


def test_play_sound_reliable_flag():
    recorder = _Recorder()
    play_sound(recorder, 33, position=(1, 2, 3))
    play_sound(recorder, 33, position=(1, 2, 3), reliable=False)
    play_sound_to(recorder, 33, reliable=False)
    assert recorder.calls[0][1].get("reliable", True) is True
    assert recorder.calls[1][1]["reliable"] is False
    assert recorder.calls[2][1]["reliable"] is False


def test_shoot_feedback_and_block_hit_sound_are_unreliable():
    server, player, _ = _server_player(C.RIFLE_TOOL)
    player.orientation = (1.0, 0.0, 0.0)
    calls = []
    original = server.broadcast

    def broadcast(data, *args, **kwargs):
        calls.append((data[0], kwargs.get("reliable", True)))
        return original(data, *args, **kwargs)

    server.broadcast = broadcast
    server.combat.handle_shot(player, _dig_packet(player, direction=(1.0, 0.0, 0.0)))
    feedback = [reliable for pid, reliable in calls if pid == ShootFeedbackPacket.id]
    assert feedback == [False]

    calls.clear()
    server.combat._broadcast_block_damage(player, (101, 100, 62), 1.0, damage_type=int(C.SPADE_DAMAGE))
    from shared.packet import Damage, PlaySound
    assert (Damage.id, True) in calls
    assert (PlaySound.id, False) in calls


def test_prefab_queue_has_a_per_player_share():
    from server.prefab_actions import PrefabActionService

    server, player, _ = _server_player(C.PREFAB_TOOL)
    _server, other, _ = _server_player(
        C.PREFAB_TOOL, player_id=1, team=TEAM2, position=(120.5, 120.5, 59.75),
        server=server,
    )
    service = PrefabActionService(server)
    player.blocks = other.blocks = 1000
    limit = PrefabActionService.PER_PLAYER_PENDING_LIMIT

    def enqueue(owner, index):
        cell = (100 + index, 104, 61)
        return service._enqueue(
            owner, name="x", anchor=cell, yaw=0,
            cells=[(cell, (1, 2, 3))], action_loop=1, reservation=None,
            infinite=False,
        )

    assert all(enqueue(player, index) for index in range(limit))
    assert enqueue(player, limit) is False
    # Another player's placement is not starved by the first one's spam.
    assert enqueue(other, 0) is True
