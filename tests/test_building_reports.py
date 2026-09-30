"""Player reports about building and digging (wave 8, 2026-09-29).

Beta 0.1 players reported that the native client (a) still placed blocks with
an empty wallet and (b) let a zombie dig twice in one swing by pressing both
mouse buttons a few milliseconds apart. The server is the authority for both:
these tests pin what it accepts whatever a client sends.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import shared.constants as C
import server.combat_runtime as combat_runtime
from protocol.packet_handler import PacketHandler
from server.combat_runtime import get_combat_system
from server.game_constants import TEAM1, TEAM2
from shared.bytes import ByteReader
from shared.packet import (
    BlockBuild,
    BlockBuildColored,
    BlockLine,
    Damage,
    PlaySound,
)

from tests.test_reversed_combat import (
    TEST_COLOR,
    DummyServer,
    make_player,
    make_shoot_packet,
)

SND_BUILD = 46


def _builder(blocks: int):
    server = DummyServer()
    repaired = []
    server.terrain_repair = SimpleNamespace(
        record_cells=lambda cells: repaired.extend(tuple(cells))
    )
    player, connection = make_player(
        server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0)
    )
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = blocks
    # Ground under the line: a block must face-touch a solid.
    for x in range(101, 105):
        server.world_manager.set_block(x, 100, 61, True, TEST_COLOR)
    return server, player, connection, repaired


def _line(player, start, end):
    packet = BlockLine()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x1, packet.y1, packet.z1 = start
    packet.x2, packet.y2, packet.z2 = end
    return packet


def _sounds(server):
    return [
        (PlaySound(ByteReader(data[1:])), exclude)
        for data, exclude in zip(server.broadcast_packets, server.broadcast_excludes)
        if data and data[0] == PlaySound.id
    ]


def _terrain_packets(server, connection):
    wanted = {BlockLine.id, BlockBuild.id, BlockBuildColored.id}
    sent = [data for data in connection.sent_packets if data and data[0] in wanted]
    sent += [data for data in server.broadcast_packets if data and data[0] in wanted]
    return sent


def test_block_line_with_an_empty_wallet_places_nothing():
    server, player, connection, repaired = _builder(blocks=0)
    cell = (101, 100, 60)

    asyncio.run(PacketHandler(server).handle(
        player, bytes(_line(player, cell, cell).generate())
    ))

    assert not server.world_manager.get_solid(*cell)
    assert player.blocks == 0
    assert _terrain_packets(server, connection) == []
    assert _sounds(server) == []
    # A client that drew the block anyway is corrected.
    assert repaired == [cell]


def test_single_block_build_with_an_empty_wallet_places_nothing():
    server, player, connection, repaired = _builder(blocks=0)
    cell = (101, 100, 60)
    packet = BlockBuild()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x, packet.y, packet.z = cell
    packet.block_type = 0

    assert get_combat_system(server).handle_block_build(player, packet) is False

    assert not server.world_manager.get_solid(*cell)
    assert player.blocks == 0
    assert _terrain_packets(server, connection) == []
    assert repaired == [cell]


@pytest.mark.parametrize("line", [False, True], ids=["single", "line"])
@pytest.mark.parametrize("blocks", [0, 1])
def test_infinite_team_waives_cost_but_still_requires_a_positive_wallet(line, blocks):
    server, player, connection, repaired = _builder(blocks=blocks)
    server.teams = {TEAM1: SimpleNamespace(infinite_blocks=True)}
    cell = (101, 100, 60)
    combat = get_combat_system(server)
    if line:
        accepted = combat.handle_block_line(player, _line(player, cell, cell))
    else:
        packet = BlockBuild()
        packet.loop_count = 1
        packet.player_id = player.id
        packet.x, packet.y, packet.z = cell
        packet.block_type = 0
        accepted = combat.handle_block_build(player, packet)
    assert accepted is (blocks > 0)
    assert bool(server.world_manager.get_solid(*cell)) is (blocks > 0)
    assert player.blocks == blocks
    assert repaired == ([] if blocks else [cell])


def test_a_line_longer_than_the_wallet_is_refused_whole():
    server, player, connection, repaired = _builder(blocks=2)

    asyncio.run(PacketHandler(server).handle(
        player, bytes(_line(player, (101, 100, 60), (103, 100, 60)).generate())
    ))

    assert not any(server.world_manager.get_solid(x, 100, 60) for x in range(101, 104))
    assert player.blocks == 2
    assert _terrain_packets(server, connection) == []
    assert repaired == [(101, 100, 60), (102, 100, 60), (103, 100, 60)]


def test_the_last_block_is_placed_charged_and_heard_by_everyone():
    server, player, connection, repaired = _builder(blocks=1)
    cell = (101, 100, 60)

    asyncio.run(PacketHandler(server).handle(
        player, bytes(_line(player, cell, cell).generate())
    ))

    assert server.world_manager.get_solid(*cell)
    assert player.blocks == 0
    assert repaired == []
    # The builder's echo is the native BlockLine.
    assert [data[0] for data in connection.sent_packets if data[0] == BlockLine.id] == [
        BlockLine.id
    ]
    # SOUND_MAP 46 (`build`) at the block, builder included: no retail
    # client script plays BUILD_SOUND for a successful placement.
    sounds = _sounds(server)
    assert len(sounds) == 1
    sound, exclude = sounds[0]
    assert sound.sound_id == SND_BUILD
    assert sound.positioned
    assert (sound.x, sound.y, sound.z) == (101.0, 100.0, 60.0)
    assert exclude is None

    # The wallet is now empty: the next block is refused.
    follow_up = (102, 100, 60)
    player._last_block_build_at = None
    asyncio.run(PacketHandler(server).handle(
        player, bytes(_line(player, follow_up, follow_up).generate())
    ))
    assert not server.world_manager.get_solid(*follow_up)
    assert player.blocks == 0


DIGGING_TOOLS = [
    ("zombie hand", "ZOMBIEHAND_TOOL", "CLASS_ZOMBIE"),
    ("spade", "SPADE_TOOL", "CLASS_SOLDIER"),
    ("pickaxe", "PICKAXE_TOOL", "CLASS_MINER"),
    ("super spade", "SUPERSPADE_TOOL", "CLASS_MINER"),
    ("knife", "KNIFE_TOOL", "CLASS_SCOUT"),
    ("crowbar", "CROWBAR_TOOL", "CLASS_GANGSTER_1"),
    ("machete", "MACHETE_TOOL", "CLASS_SPECIALIST"),
    ("classic spade", "CLASSIC_SPADE_TOOL", "CLASS_CLASSIC_SOLDIER"),
]


def _digger(tool_name: str, class_name: str):
    server = DummyServer()
    tool = int(getattr(C, tool_name))
    player, _ = make_player(
        server, 0, "Digger", TEAM2, tool, (100.5, 100.5, 60.0)
    )
    player.class_id = int(getattr(C, class_name))
    player.loadout = [tool]
    player.set_tool(tool, raw=True)
    player.blocks = 0
    for x in range(101, 110):
        for y in range(97, 104):
            for z in range(56, 64):
                server.world_manager.set_block(x, y, z, True, TEST_COLOR)
    server.world_manager.find_unsupported_chunks = lambda _frontier: []
    return server, player


def _swing(server, player, clock, at, *, secondary=False, seed=1):
    clock[0] = at
    packet = make_shoot_packet(player, orientation=(1.0, 0.0, 0.0), seed=seed)
    packet.secondary = 1 if secondary else 0
    return get_combat_system(server).handle_shot(player, packet)


@pytest.mark.parametrize("secondary", [False, True], ids=["primary", "secondary"])
@pytest.mark.parametrize("gap", [0.0, 0.005, 0.05, 0.2])
@pytest.mark.parametrize(
    "label,tool_name,class_name", DIGGING_TOOLS, ids=[row[0] for row in DIGGING_TOOLS]
)
def test_two_dig_packets_inside_one_swing_dig_once(
    monkeypatch, label, tool_name, class_name, gap, secondary
):
    """Both mouse buttons a few milliseconds apart are one swing."""

    server, player = _digger(tool_name, class_name)
    clock = [1000.0]
    monkeypatch.setattr(combat_runtime.time, "monotonic", lambda: clock[0])
    interval = float(player.get_weapon_profile().fire_interval)
    assert gap < interval

    assert _swing(server, player, clock, 1000.0) is True
    blocks_after_first = player.blocks
    damage_after_first = [d for d in server.broadcast_packets if d[0] == Damage.id]

    assert _swing(
        server, player, clock, 1000.0 + gap, secondary=secondary, seed=2
    ) is False

    assert player.blocks == blocks_after_first
    assert [
        d for d in server.broadcast_packets if d[0] == Damage.id
    ] == damage_after_first
    assert len(damage_after_first) == 1


@pytest.mark.parametrize(
    "label,tool_name,class_name", DIGGING_TOOLS, ids=[row[0] for row in DIGGING_TOOLS]
)
def test_alternating_buttons_cannot_beat_the_swing_interval(
    monkeypatch, label, tool_name, class_name
):
    """Two interleaved packet streams still dig once per shoot_interval."""

    server, player = _digger(tool_name, class_name)
    clock = [2000.0]
    monkeypatch.setattr(combat_runtime.time, "monotonic", lambda: clock[0])
    interval = float(player.get_weapon_profile().fire_interval)

    duration = 6.0
    step = 0
    # LMB stream on the interval, RMB stream 5 ms behind it.
    while step * interval < duration:
        at = 2000.0 + step * interval
        _swing(server, player, clock, at, seed=step + 1)
        digs_before = len([d for d in server.broadcast_packets if d[0] == Damage.id])
        schedule_before = player.next_shot_time
        # The second button never digs and never moves the swing schedule.
        assert _swing(
            server, player, clock, at + 0.005, secondary=True, seed=step + 101
        ) is False
        assert len(
            [d for d in server.broadcast_packets if d[0] == Damage.id]
        ) == digs_before
        assert player.next_shot_time == schedule_before
        step += 1

    digs = len([d for d in server.broadcast_packets if d[0] == Damage.id])
    assert 1 <= digs <= step


def test_a_tool_switch_does_not_reset_the_swing_clock(monkeypatch):
    server, player = _digger("SPADE_TOOL", "CLASS_MINER")
    player.loadout = [int(C.SPADE_TOOL), int(C.PICKAXE_TOOL)]
    clock = [3000.0]
    monkeypatch.setattr(combat_runtime.time, "monotonic", lambda: clock[0])

    assert _swing(server, player, clock, 3000.0) is True
    player.set_tool(int(C.PICKAXE_TOOL), raw=True)
    assert _swing(server, player, clock, 3000.05, seed=2) is False
