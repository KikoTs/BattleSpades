"""Regression coverage for pre-existing Drill holes on reconnect."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.config import ServerConfig
from server.main import BattleSpadesServer
from server.projectiles import drill_contact_cells
from server.runtime_vxl import ServerVXL
from shared.bytes import ByteReader
from shared.packet import BlockBuildColored, Damage


class _Joiner:
    """Minimal post-GameScene connection used by exact-air catch-up."""

    def __init__(self) -> None:
        self.in_game = False
        self.player = SimpleNamespace(id=7, team=0)
        self.map_mutation_watermark = None
        self.map_mutation_overflow = False
        self.map_cell_watermark = None
        self.map_cell_overflow = False
        self.map_cell_replay = None
        self.map_air_replay = None
        self.sent: list[bytes] = []

    def send(self, data: bytes, reliable: bool = True) -> None:
        assert reliable is True
        self.sent.append(bytes(data))


class _FailOnceJoiner(_Joiner):
    """Reliable peer whose first exact-air send is rejected by ENet."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_send = True

    def send(self, data: bytes, reliable: bool = True) -> None:
        if self.fail_next_send:
            self.fail_next_send = False
            raise RuntimeError("synthetic ENet queue failure")
        super().send(data, reliable=reliable)


def _damage_cell(data: bytes) -> tuple[int, int, int]:
    packet = Damage(ByteReader(data[1:]))
    assert packet.type == 6
    assert packet.chunk_check == 0
    return tuple(int(value + 0.5) for value in packet.position)


def test_preexisting_drill_hole_replays_every_air_cell_in_bounded_batches() -> None:
    """Disconnect/rejoin must not rely only on the native VXL column merge."""

    config = ServerConfig(map_air_catchup_batch_limit=17)
    server = BattleSpadesServer(config)
    world = server.world_manager
    world.map = ServerVXL(-1, b"", 0, 2)

    # Build a solid volume, then bore two overlapping measured Drill contacts
    # before the joining client takes its MapSync snapshot.
    volume = [
        (x, y, z)
        for x in range(96, 107)
        for y in range(96, 105)
        for z in range(96, 105)
    ]
    for cell in volume:
        world.set_block(*cell, True, 0x315A27)
    expected = set(
        world.destroy_blocks(
            list(drill_contact_cells((101, 100, 100)))
            + list(drill_contact_cells((103, 100, 100)))
        )
    )
    assert len(expected) > config.map_air_catchup_batch_limit

    joiner = _Joiner()
    server.connections = {"joining": joiner}
    server.mark_map_snapshot_complete(joiner)

    # The first frame is bounded and must keep gameplay gated.
    assert server.replay_map_air_overrides(joiner) is False
    assert len(joiner.sent) == config.map_air_catchup_batch_limit

    attempts = 1
    while not server.replay_map_air_overrides(joiner):
        attempts += 1
        assert attempts < 100

    repaired = {_damage_cell(data) for data in joiner.sent}
    assert repaired == expected
    assert len(joiner.sent) == len(expected)
    assert joiner.map_air_replay is None


def test_rebuilt_voxel_is_removed_from_future_reconnect_air_masks() -> None:
    """A later build must not be cleared by the persistent Drill safety net."""

    config = SimpleNamespace(maps_path="maps", game_mode="tdm")
    from server.world_manager import WorldManager

    world = WorldManager(config)
    world.map = ServerVXL(-1, b"", 0, 2)
    cell = (40, 50, 60)
    world.set_block(*cell, True, 0x224466)
    assert world.destroy_blocks([cell]) == [cell]
    assert world.snapshot_air_overrides()

    world.set_block(*cell, True, 0x6688AA)
    assert world.snapshot_air_overrides() == ()


def test_air_replay_retry_keeps_the_first_unaccepted_voxel() -> None:
    """A failed reliable send must not advance the compact bitmask cursor."""

    server = BattleSpadesServer(ServerConfig(map_air_catchup_batch_limit=8))
    world = server.world_manager
    world.map = ServerVXL(-1, b"", 0, 2)
    cell = (22, 33, 44)
    world.set_block(*cell, True, 0x335577)
    world.destroy_blocks([cell])

    joiner = _FailOnceJoiner()
    server.connections = {"joining": joiner}
    server.mark_map_snapshot_complete(joiner)
    lease = joiner.map_air_replay
    with pytest.raises(RuntimeError, match="ENet queue"):
        server.replay_map_air_overrides(joiner)

    assert joiner.map_air_replay is lease
    assert lease.remaining_mask & (1 << cell[2])
    assert server.replay_map_air_overrides(joiner) is True
    assert [_damage_cell(data) for data in joiner.sent] == [cell]


def test_mutation_during_batched_air_replay_finishes_at_current_vxl_state() -> None:
    """Post-snapshot builds/removals must win over the frozen air masks."""

    server = BattleSpadesServer(ServerConfig(map_air_catchup_batch_limit=1))
    world = server.world_manager
    world.map = ServerVXL(-1, b"", 0, 2)
    rebuilt = (50, 60, 70)
    later_removed = (51, 60, 70)
    world.set_block(*rebuilt, True, 0x112233)
    world.set_block(*later_removed, True, 0x223344)
    world.destroy_blocks([rebuilt])

    joiner = _Joiner()
    server.connections = {"joining": joiner}
    server.mark_map_snapshot_complete(joiner)

    # These commits happen while the native client is building/draining its
    # MapSync. The frozen mask mentions only ``rebuilt``; canonical reads and
    # the cross-boundary journal must still produce the final state.
    world.set_block(*rebuilt, True, 0xAABBCC)
    world.destroy_blocks([later_removed])

    assert server.replay_map_air_overrides(joiner) is True
    assert joiner.sent[0][0] == BlockBuildColored.id
    server.replay_map_mutations(joiner)

    packet_ids = [data[0] for data in joiner.sent]
    assert packet_ids.count(BlockBuildColored.id) >= 1
    damage_cells = {
        _damage_cell(data) for data in joiner.sent if data[0] == Damage.id
    }
    assert later_removed in damage_cells
    assert rebuilt not in damage_cells
