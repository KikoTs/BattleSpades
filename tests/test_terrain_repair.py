from types import SimpleNamespace

import shared.constants as C
from shared.bytes import ByteReader
from server.config import ServerConfig
from server.main import BattleSpadesServer
from server.runtime_vxl import ServerVXL
from server.world_manager import WorldManager
from shared.packet import BlockBuildColored, Damage


class RecordingConnection:
    def __init__(self, player_id=3, *, in_game=True):
        self.player = SimpleNamespace(id=player_id)
        self.in_game = in_game
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append((bytes(data), reliable))


class FakeWorld:
    def __init__(self):
        self.cells = {}

    @staticmethod
    def _valid_block_position(x, y, z):
        return 0 <= x < 512 and 0 <= y < 512 and 0 <= z < 240

    def get_solid(self, x, y, z):
        return (x, y, z) in self.cells

    def get_color(self, x, y, z):
        return self.cells[(x, y, z)]


def _server_with_fast_repair():
    config = ServerConfig(
        terrain_repair_delay_ticks=2,
        terrain_repair_interval_ticks=1,
        terrain_repair_batch_limit=8,
    )
    server = BattleSpadesServer(config)
    server.world_manager = FakeWorld()
    connection = RecordingConnection()
    server.connections = {object(): connection}
    return server, connection


def test_repair_reads_latest_canonical_color_after_quiet_delay():
    server, connection = _server_with_fast_repair()
    cell = (10, 20, 30)
    server.world_manager.cells[cell] = 0x80112233
    server.terrain_repair.record_cells([cell])

    # A second edit supersedes queued state. The service retains only the cell,
    # then reads its canonical VXL color at send time.
    server.world_manager.cells[cell] = 0x80A1B2C3
    server.loop_count = 1
    assert server.terrain_repair.tick() == 0
    server.loop_count = 2
    assert server.terrain_repair.tick() == 1

    raw, reliable = connection.sent[0]
    packet = BlockBuildColored(ByteReader(raw[1:]))
    assert raw[0] == 33
    assert reliable is True
    assert (packet.x, packet.y, packet.z) == cell
    assert packet.color == 0xA1B2C3
    assert server.terrain_repair.pending_count == 0


def test_repair_uses_exact_non_collapsing_damage_for_canonical_air():
    server, connection = _server_with_fast_repair()
    cell = (40, 50, 60)
    server.terrain_repair.record_cells([cell])
    server.loop_count = 2
    assert server.terrain_repair.tick() == 1

    raw, reliable = connection.sent[0]
    packet = Damage(ByteReader(raw[1:]))
    assert raw[0] == 37
    assert reliable is True
    assert packet.type == int(C.WEAPON_DAMAGE)
    assert packet.chunk_check == 0
    assert packet.damage == 31.75
    assert packet.position == tuple(float(value) for value in cell)


def test_mid_join_connection_does_not_receive_repair_packets():
    server, connection = _server_with_fast_repair()
    connection.in_game = False
    server.terrain_repair.record_cells([(1, 2, 3)])
    server.loop_count = 2

    assert server.terrain_repair.tick() == 0
    assert connection.sent == []
    assert server.terrain_repair.pending_count == 0


def test_repair_uses_each_recipients_guaranteed_local_player_id():
    server, first = _server_with_fast_repair()
    second = RecordingConnection(player_id=9)
    server.connections = {"first": first, "second": second}
    cell = (8, 9, 10)
    server.world_manager.cells[cell] = 0x123456
    server.terrain_repair.record_cells([cell])
    server.loop_count = 2

    assert server.terrain_repair.tick() == 1
    first_packet = BlockBuildColored(ByteReader(first.sent[0][0][1:]))
    second_packet = BlockBuildColored(ByteReader(second.sent[0][0][1:]))
    assert first_packet.player_id == first.player.id
    assert second_packet.player_id == second.player.id


def test_accepted_world_mutations_do_not_schedule_duplicate_visual_replays():
    """A successful gameplay packet is the only visual announcement needed.

    Terrain repair exists for rejected client predictions. Enrolling every
    accepted mutation makes the native client run its add/damage callback a
    second time after the quiet delay, including placement particles.
    """
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.map = ServerVXL(-1, b"", 0, 2)

    assert server.world_manager.set_block(2, 3, 4, True, 0x123456) is True
    assert server.terrain_repair.pending_count == 0
    assert server.world_manager.destroy_blocks(
        [(2, 3, 4), (2, 3, 4)]
    ) == [(2, 3, 4)]
    assert server.terrain_repair.pending_count == 0


def test_production_batch_is_bounded_with_fifty_clients():
    config = ServerConfig(
        terrain_repair_delay_ticks=1,
        terrain_repair_interval_ticks=1,
        terrain_repair_batch_limit=4,
    )
    server = BattleSpadesServer(config)
    server.world_manager = FakeWorld()
    connections = [RecordingConnection(player_id=index) for index in range(50)]
    server.connections = {index: connection for index, connection in enumerate(connections)}
    cells = [(index, 2, 3) for index in range(10)]
    server.terrain_repair.record_cells(cells)
    server.loop_count = 1

    assert server.terrain_repair.tick() == 4
    assert server.terrain_repair.pending_count == 6
    assert sum(len(connection.sent) for connection in connections) == 200
    assert server.metrics.terrain_repair_cells == 4
    assert server.metrics.terrain_repair_sends == 200
