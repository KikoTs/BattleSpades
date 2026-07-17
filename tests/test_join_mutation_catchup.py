import asyncio
from types import SimpleNamespace

import shared.constants as C
from server.config import ServerConfig
from server.connection import Connection
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from server.player import Player
from shared.bytes import ByteReader
from shared.packet import (
    BlockBuild,
    BlockBuildColored,
    ClientData,
    CreatePlayer,
    Damage,
    MapDataValidation,
    SetColor,
    WorldUpdate,
)


class RecordingConnection:
    def __init__(self, in_game=False):
        self.in_game = in_game
        self.player = SimpleNamespace(id=9, team=0)
        self.sent = []
        self.map_mutation_watermark = None

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))


class DisconnectRecordingConnection(RecordingConnection):
    def __init__(self, in_game=False):
        super().__init__(in_game=in_game)
        self.disconnect_reason = None

    def disconnect(self, reason=0):
        self.disconnect_reason = reason


class FailSecondSendConnection(RecordingConnection):
    def __init__(self):
        super().__init__(in_game=False)
        self.attempts = 0

    def send(self, data, reliable=True, prefix=0x30):
        self.attempts += 1
        if self.attempts == 2:
            raise RuntimeError("synthetic send failure")
        super().send(data, reliable=reliable, prefix=prefix)


class CanonicalRecordingConnection(RecordingConnection):
    """Join fixture opting into the production topology catch-up path."""

    def __init__(self, player_id=9):
        super().__init__(in_game=False)
        self.player = SimpleNamespace(id=player_id, team=0)
        self.map_cell_watermark = None
        self.map_cell_overflow = False
        self.map_cell_replay = None


class FailSecondCanonicalSendConnection(CanonicalRecordingConnection):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    def send(self, data, reliable=True, prefix=0x30):
        self.attempts += 1
        if self.attempts == 2:
            raise RuntimeError("synthetic canonical send failure")
        super().send(data, reliable=reliable, prefix=prefix)


def _block_build_bytes(x=10, y=20, z=30):
    packet = BlockBuild()
    packet.loop_count = 1
    packet.player_id = 1
    packet.x, packet.y, packet.z = x, y, z
    packet.block_type = 0
    return bytes(packet.generate())


def _damage_bytes(x=10.0, y=20.0, z=30.0):
    packet = Damage()
    packet.player_id = 1
    packet.type = 6
    packet.damage = 31.75
    packet.face = 0
    packet.chunk_check = 1
    packet.seed = 0
    packet.causer_id = 1
    packet.position = (x, y, z)
    return bytes(packet.generate())


def test_joiner_replays_build_and_destroy_after_map_snapshot():
    server = BattleSpadesServer(ServerConfig())
    joiner = RecordingConnection(in_game=False)
    server.connections = {9: joiner}

    # This watermark represents the exact world state serialized into the
    # joiner's MapSync stream. Later gameplay broadcasts are gated while the
    # client constructs GameScene, so they must be journaled for catch-up.
    server.mark_map_snapshot_complete(joiner)
    build = _block_build_bytes()
    destroy = _damage_bytes()
    server.broadcast(build)
    server.broadcast(destroy)

    assert joiner.sent == []
    server.replay_map_mutations(joiner)
    assert joiner.sent == [build, destroy]


def test_canonical_join_replay_coalesces_repeated_cell_edits_to_final_color():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    joiner = CanonicalRecordingConnection()
    server.connections = {9: joiner}
    server.mark_map_snapshot_complete(joiner)

    cell = (10, 20, 61)
    assert server.world_manager.set_block(*cell, True, 0x112233)
    assert server.world_manager.set_block(*cell, True, 0x445566)
    assert server.world_manager.set_block(*cell, False)
    assert server.world_manager.set_block(*cell, True, 0xA1B2C3)

    server.replay_map_mutations(joiner)

    assert len(joiner.sent) == 1
    packet = BlockBuildColored(ByteReader(joiner.sent[0][1:]))
    assert packet.player_id == joiner.player.id
    assert (packet.x, packet.y, packet.z) == cell
    assert packet.color == 0xA1B2C3


def test_canonical_coalescing_preserves_supported_build_order_after_recolor():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    joiner = CanonicalRecordingConnection()
    server.connections = {9: joiner}
    server.mark_map_snapshot_complete(joiner)
    base = (20, 20, 61)
    extension = (20, 20, 60)

    assert server.world_manager.set_block(*base, True, 0x101010)
    assert server.world_manager.set_block(*extension, True, 0x202020)
    assert server.world_manager.set_block(*base, True, 0x303030)
    server.replay_map_mutations(joiner)

    packets = [
        BlockBuildColored(ByteReader(data[1:])) for data in joiner.sent
    ]
    assert [
        (packet.x, packet.y, packet.z) for packet in packets
    ] == [base, extension]
    assert packets[0].color == 0x303030


def test_canonical_join_replay_expands_collapse_to_exact_air_cells():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    joiner = CanonicalRecordingConnection()
    server.connections = {9: joiner}
    cells = [(30, 30, 61), (30, 30, 60), (31, 30, 60)]
    for cell in cells:
        assert server.world_manager.set_block(*cell, True, 0x334455)
    server.mark_map_snapshot_complete(joiner)

    assert server.world_manager.destroy_blocks(cells) == cells
    server.replay_map_mutations(joiner)

    packets = [Damage(ByteReader(data[1:])) for data in joiner.sent]
    assert len(packets) == len(cells)
    assert {tuple(int(value) for value in packet.position) for packet in packets} == set(cells)
    assert all(packet.chunk_check == 0 for packet in packets)
    assert all(packet.player_id == joiner.player.id for packet in packets)


def test_canonical_join_snapshot_excludes_earlier_edits_and_catches_later_ones():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    joiner = CanonicalRecordingConnection()
    server.connections = {9: joiner}

    before = (40, 40, 61)
    after = (41, 40, 61)
    assert server.world_manager.set_block(*before, True, 0x102030)
    assert list(server._map_cell_journal) == []
    server.mark_map_snapshot_complete(joiner)
    assert server.world_manager.set_block(*after, True, 0x405060)

    server.replay_map_mutations(joiner)
    packets = [BlockBuildColored(ByteReader(data[1:])) for data in joiner.sent]
    assert [(packet.x, packet.y, packet.z) for packet in packets] == [after]


def test_simultaneous_canonical_joiners_keep_independent_topology_watermarks():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    first = CanonicalRecordingConnection(player_id=8)
    second = CanonicalRecordingConnection(player_id=9)
    server.connections = {8: first, 9: second}
    server.mark_map_snapshot_complete(first)
    cell_a = (50, 50, 61)
    cell_b = (51, 50, 61)
    assert server.world_manager.set_block(*cell_a, True, 0x111111)
    server.mark_map_snapshot_complete(second)
    assert server.world_manager.set_block(*cell_b, True, 0x222222)

    server.replay_map_mutations(second)
    server.replay_map_mutations(first)

    second_cells = {
        tuple(
            getattr(BlockBuildColored(ByteReader(data[1:])), name)
            for name in ("x", "y", "z")
        )
        for data in second.sent
    }
    first_cells = {
        tuple(
            getattr(BlockBuildColored(ByteReader(data[1:])), name)
            for name in ("x", "y", "z")
        )
        for data in first.sent
    }
    assert second_cells == {cell_b}
    assert first_cells == {cell_a, cell_b}


def test_canonical_replay_retry_resumes_inside_multi_cell_collapse():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    joiner = FailSecondCanonicalSendConnection()
    server.connections = {9: joiner}
    cells = [(60, 60, 61), (60, 60, 60), (61, 60, 60)]
    for cell in cells:
        assert server.world_manager.set_block(*cell, True, 0x778899)
    server.mark_map_snapshot_complete(joiner)
    assert server.world_manager.destroy_blocks(cells) == cells

    try:
        server.replay_map_mutations(joiner)
    except RuntimeError:
        pass
    server.replay_map_mutations(joiner)

    assert len(joiner.sent) == 3
    assert all(
        Damage(ByteReader(data[1:])).chunk_check == 0
        for data in joiner.sent
    )
    assert joiner.map_cell_replay is None


def test_new_snapshot_does_not_replay_mutations_already_in_its_columns():
    server = BattleSpadesServer(ServerConfig())
    first_joiner = RecordingConnection(in_game=False)
    server.connections = {9: first_joiner}
    server.mark_map_snapshot_complete(first_joiner)
    old_mutation = _block_build_bytes(1, 2, 3)
    server.broadcast(old_mutation)

    second_joiner = RecordingConnection(in_game=False)
    server.connections[10] = second_joiner
    server.mark_map_snapshot_complete(second_joiner)
    new_mutation = _block_build_bytes(4, 5, 6)
    server.broadcast(new_mutation)

    server.replay_map_mutations(second_joiner)
    assert second_joiner.sent == [new_mutation]


def test_palette_updates_are_not_journaled_as_terrain_mutations():
    """SetColor is player state, not a voxel mutation.

    A joiner receives one authoritative palette snapshot during reveal.  If an
    older SetColor is retained in the terrain journal and replayed afterward,
    it can overwrite that snapshot with a stale held-block colour.
    """
    server = BattleSpadesServer(ServerConfig())
    joiner = RecordingConnection(in_game=False)
    server.connections = {9: joiner}
    server.mark_map_snapshot_complete(joiner)

    color = SetColor()
    color.player_id = 1
    color.value = 0x123456
    sequence_before = server._map_mutation_sequence
    server.broadcast(bytes(color.generate()))

    assert server._map_mutation_sequence == sequence_before
    assert list(server._map_mutation_journal) == []


def test_simultaneous_joiners_receive_each_other_at_first_frame_reveal():
    """Both map handshakes can snapshot the roster before either player exists.

    Their later CreatePlayer broadcasts are gameplay-gated, so reveal must
    send each missing life exactly once or the clients remain mutually
    invisible for the entire match.
    """

    from server.roster import catch_up_roster, remember_player_life

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    first = RecordingConnection(in_game=False)
    second = RecordingConnection(in_game=False)
    first.server = server
    second.server = server
    first_player = Player(0, "First", TEAM1, C.RIFLE_TOOL, first)
    second_player = Player(1, "Second", TEAM1, C.RIFLE_TOOL, second)
    first.player = first_player
    second.player = second_player
    first_player.spawn(100.5, 100.5, 59.75)
    second_player.spawn(110.5, 100.5, 59.75)
    server.players = {0: first_player, 1: second_player}

    # Each direct self CreatePlayer was delivered, while the other player's
    # gated broadcast was missed.
    remember_player_life(first, first_player)
    remember_player_life(second, second_player)
    catch_up_roster(server, first)
    catch_up_roster(server, second)

    first_creates = [data for data in first.sent if data[0] == CreatePlayer.id]
    second_creates = [data for data in second.sent if data[0] == CreatePlayer.id]
    assert len(first_creates) == len(second_creates) == 1
    assert CreatePlayer(ByteReader(first_creates[0][1:])).player_id == 1
    assert CreatePlayer(ByteReader(second_creates[0][1:])).player_id == 0

    catch_up_roster(server, first)
    catch_up_roster(server, second)
    assert len([data for data in first.sent if data[0] == CreatePlayer.id]) == 1
    assert len([data for data in second.sent if data[0] == CreatePlayer.id]) == 1


def test_reveal_reliably_initializes_remote_current_tool_and_action_state():
    """CreatePlayer's loadout default must not linger until packet-loss luck."""

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    joiner = RecordingConnection(in_game=False)
    joiner.server = server
    local = Player(9, "Joining", TEAM1, C.RIFLE_TOOL, joiner)
    remote_connection = RecordingConnection(in_game=True)
    remote_connection.server = server
    remote = Player(1, "Builder", TEAM1, C.RIFLE_TOOL, remote_connection)
    joiner.player = local
    remote_connection.player = remote
    local.spawn(100.5, 100.5, 59.75)
    remote.loadout = [int(C.RIFLE_TOOL), int(C.BLOCK_TOOL)]
    remote.spawn(110.5, 100.5, 59.75)
    remote.set_tool(int(C.BLOCK_TOOL), raw=True)
    remote.input.can_display_weapon = True
    server.players = {local.id: local, remote.id: remote}
    server.connections = {"joiner": joiner, "remote": remote_connection}

    server.reveal_world_to(joiner)

    snapshots = [data for data in joiner.sent if data[0] == WorldUpdate.id]
    assert len(snapshots) == 1
    parsed = WorldUpdate(ByteReader(snapshots[0][1:]))
    assert local.id not in parsed.player_updates
    row = parsed.player_updates[remote.id]
    assert row[9] == int(C.BLOCK_TOOL)
    assert row[7] & 0x10


def test_replay_retry_resumes_after_last_successful_mutation():
    server = BattleSpadesServer(ServerConfig())
    joiner = FailSecondSendConnection()
    server.connections = {9: joiner}
    server.mark_map_snapshot_complete(joiner)
    first = _block_build_bytes(1, 2, 3)
    second = _block_build_bytes(4, 5, 6)
    server.broadcast(first)
    server.broadcast(second)

    try:
        server.replay_map_mutations(joiner)
    except RuntimeError:
        pass
    server.replay_map_mutations(joiner)

    assert joiner.sent == [first, second]


def test_pending_join_disconnect_releases_retained_journal():
    server = BattleSpadesServer(ServerConfig())
    peer = object()
    joiner = RecordingConnection(in_game=False)
    joiner.reserved_player_id = None
    joiner.player = None
    joiner.on_disconnect = lambda: None
    server.connections = {peer: joiner}
    server.mark_map_snapshot_complete(joiner)
    server.broadcast(_block_build_bytes())
    assert server._map_mutation_journal

    server._on_disconnect_sync(peer)

    assert list(server._map_mutation_journal) == []


def test_pending_canonical_join_disconnect_releases_exact_cell_journal():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    peer = object()
    joiner = CanonicalRecordingConnection()
    joiner.reserved_player_id = None
    joiner.on_disconnect = lambda: None
    server.connections = {peer: joiner}
    server.mark_map_snapshot_complete(joiner)
    assert server.world_manager.set_block(70, 70, 61, True, 0x123456)
    assert server._map_cell_journal
    joiner.player = None

    server._on_disconnect_sync(peer)

    assert list(server._map_cell_journal) == []


def test_world_replacement_rebinds_exact_cell_listener_and_rollback_restores_it():
    server = BattleSpadesServer(ServerConfig())
    original = server.world_manager
    original.generate_flat_map()
    replacement = type(original)(server.config)
    replacement.generate_flat_map()
    joiner = CanonicalRecordingConnection()
    server.connections = {9: joiner}
    server.mark_map_snapshot_complete(joiner)

    original.set_block(80, 80, 61, True, 0x111111)
    assert server._map_cell_sequence == 1
    server.world_manager = replacement
    server._bind_world_mutation_journal()
    original.set_block(81, 80, 61, True, 0x222222)
    assert server._map_cell_sequence == 1
    replacement.set_block(82, 80, 61, True, 0x333333)
    assert server._map_cell_sequence == 2

    server.world_manager = original
    server._bind_world_mutation_journal()
    replacement.set_block(83, 80, 61, True, 0x444444)
    assert server._map_cell_sequence == 2
    original.set_block(84, 80, 61, True, 0x555555)
    assert server._map_cell_sequence == 3


def test_transition_reset_clears_exact_cell_cursor_and_connection_lease():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    joiner = CanonicalRecordingConnection()
    server.connections = {9: joiner}
    server.mark_map_snapshot_complete(joiner)
    assert server.world_manager.set_block(90, 90, 61, True, 0x123456)
    joiner.map_cell_replay = object()

    server.match_transition._reset_map_journal()

    assert list(server._map_cell_journal) == []
    assert server._map_cell_sequence == 0
    assert joiner.map_cell_watermark is None
    assert joiner.map_cell_overflow is False
    assert joiner.map_cell_replay is None


def test_disconnect_releases_replication_state_for_reused_player_id():
    server = BattleSpadesServer(ServerConfig())
    peer = object()
    forgotten: list[int] = []
    player = SimpleNamespace(id=3, name="old", team=99)
    connection = RecordingConnection(in_game=True)
    connection.reserved_player_id = None
    connection.player = player
    connection.on_disconnect = lambda: None
    server.connections = {peer: connection}
    server.players = {3: player}
    server.replication.forget_player = forgotten.append

    server._on_disconnect_sync(peer)

    assert forgotten == [3]


def test_joiner_is_rejected_instead_of_partially_replayed_after_journal_overflow():
    config = ServerConfig(max_map_mutation_journal=64)
    server = BattleSpadesServer(config)
    joiner = DisconnectRecordingConnection(in_game=False)
    server.connections = {9: joiner}
    server.mark_map_snapshot_complete(joiner)

    for index in range(65):
        server.broadcast(_block_build_bytes(index, 2, 3))

    assert joiner.map_mutation_overflow is True
    try:
        server.replay_map_mutations(joiner)
    except RuntimeError as exc:
        assert "contiguous terrain snapshot" in str(exc)
    else:
        raise AssertionError("overflowed join catch-up was admitted")

    assert joiner.disconnect_reason == 13
    assert server.metrics.map_mutation_overflows >= 1


def test_canonical_join_is_rejected_after_exact_cell_journal_overflow():
    server = BattleSpadesServer(ServerConfig(max_map_mutation_journal=64))
    server.world_manager.generate_flat_map()
    joiner = CanonicalRecordingConnection()
    joiner.disconnect_reason = None
    joiner.disconnect = lambda reason=0: setattr(
        joiner, "disconnect_reason", reason
    )
    server.connections = {9: joiner}
    server.mark_map_snapshot_complete(joiner)

    for index in range(65):
        x, y = divmod(index, 16)
        assert server.world_manager.set_block(
            100 + x, 100 + y, 61, True, 0xABCDEF
        )

    assert joiner.map_cell_overflow is True
    try:
        server.replay_map_mutations(joiner)
    except RuntimeError as exc:
        assert "contiguous terrain snapshot" in str(exc)
    else:
        raise AssertionError("overflowed canonical catch-up was admitted")
    assert joiner.disconnect_reason == 13


class DummyPeer:
    address = ("127.0.0.1", 32887)

    def disconnect(self, reason=0):
        return None


def test_real_handshake_replays_post_mapsync_mutations_before_ingame():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    peer = DummyPeer()
    connection = Connection(peer, server)
    player = Player(9, "Joining", TEAM1, C.RIFLE_TOOL, connection)
    player.spawn(100.5, 100.5, 59.75)
    connection.player = player
    server.players[player.id] = player
    server.connections[peer] = connection
    sent = []
    connection.send = lambda data, reliable=True, prefix=0x30: sent.append(bytes(data))

    async def matching_crc(packet_class, timeout=5.0):
        packet = MapDataValidation()
        crc = server.world_manager.map_file_crc
        packet.crc = crc - (1 << 32) if crc >= (1 << 31) else crc
        return packet

    connection.wait_for = matching_crc
    asyncio.run(connection.send_map_data())
    assert connection.map_mutation_watermark is not None
    sent.clear()

    build_cell = (7, 8, 61)
    destroy_cell = (9, 10, 62)
    assert server.world_manager.set_block(*build_cell, True, 0x123456)
    assert server.world_manager.destroy_blocks([destroy_cell]) == [destroy_cell]
    assert sent == []

    packet = ClientData()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.tool_id = C.RIFLE_TOOL
    packet.o_x, packet.o_y, packet.o_z = 1.0, 0.0, 0.0
    packet.ooo = 0
    packet.weapon_deployment_yaw = 0.0
    asyncio.run(connection.on_receive(bytes([0x30]) + bytes(packet.generate())))

    # Palette and the reliable remote-only roster WorldUpdate precede exact
    # canonical terrain replay.
    assert sent[0][0] == 11
    assert sent[1][0] == WorldUpdate.id
    built = BlockBuildColored(ByteReader(sent[2][1:]))
    destroyed = Damage(ByteReader(sent[3][1:]))
    assert (built.x, built.y, built.z) == build_cell
    assert built.color == 0x123456
    assert tuple(int(value) for value in destroyed.position) == destroy_cell
    assert destroyed.chunk_check == 0
    assert connection.in_game is True
