"""Diamond Mine: a round never stays without a diamond.

Retail never places a diamond on the map. Digging uncovers them, by chance:
DIA_HIGHEST_DIAMOND_CHANCE is 1 in 100 mined blocks, the start cue is "Dig
for diamonds!", and neither the map files nor the map editor know a diamond
spawn point. With bad luck (or bots that did not dig) a round stayed empty,
which players reported as "the diamond does not spawn".

``[modes.dia] discovery_guarantee_blocks`` bounds the bad luck. These tests
take the worst case - the chance roll always fails - on every map of the
official Diamond Mine rotation.
"""

from __future__ import annotations

import asyncio
import math
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.constants as C
from modes.diamond_mine import DEFAULT_DISCOVERY_GUARANTEE_BLOCKS, DiamondMineMode
from protocol.packet_handler import PacketHandler
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.player import Player
from server import action_clock
from shared.bytes import ByteReader
from shared.packet import CreateEntity, LocalisedMessage, PickPickup, ShootPacket
from tests.test_diamond_mine_fixes import _mode
from tests.test_recovered_objective_modes import _decode, _Player

ROOT = Path(__file__).resolve().parents[1]
NEVER = SimpleNamespace(random=lambda: 0.999999, randrange=lambda _n: 0)


def _rotation() -> list[str]:
    with (ROOT / "configs" / "official-diamond-mine.toml").open("rb") as stream:
        names = tomllib.load(stream)["lobby"]["map_rotation"]
    return [name for name in names if (ROOT / "maps" / f"{name}.vxl").exists()]


# -- the rule ---------------------------------------------------------------


def _miner(server, mode, now):
    miner = _Player(1, TEAM1, (120.5, 100.5, 50.5))
    miner.connection = miner
    server.players[miner.id] = miner
    asyncio.run(mode.on_mode_start())
    mode._rng = NEVER
    return miner


def _mine(mode, miner, count, start=0):
    for index in range(start, start + count):
        asyncio.run(mode.on_blocks_destroyed(
            miner, ((200 + index % 100, 300 + index // 100, 40),), True
        ))


def test_default_is_three_times_the_retail_average():
    assert DEFAULT_DISCOVERY_GUARANTEE_BLOCKS == 300
    assert ServerConfig().mode_settings.get("dia", {}).get(
        "discovery_guarantee_blocks", DEFAULT_DISCOVERY_GUARANTEE_BLOCKS
    ) == 300


def test_the_overdue_block_uncovers_a_diamond_where_it_was_mined(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    miner = _miner(server, mode, now)

    _mine(mode, miner, 299)
    assert not mode.ground_diamonds

    server.packets.clear()
    _mine(mode, miner, 1, start=299)

    assert len(mode.ground_diamonds) == 1
    diamond = next(iter(mode.ground_diamonds.values()))
    assert diamond.position == (299 % 100 + 200.5, 302.5, 40.5)
    assert [
        (line.string_id, list(line.parameters))
        for line in _decode(server.packets, LocalisedMessage)
    ] == [("DIAMOND_UNCOVERED", [miner.name])]
    entity = server.entity_registry.get(diamond.entity_id)
    assert entity.type == int(C.DIAMOND_PICKUP)
    assert server.created[-1] is entity
    assert mode._mined_since_discovery == 0


def test_bulk_removal_counts_every_block(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    miner = _miner(server, mode, now)

    for swing in range(99):
        asyncio.run(mode.on_blocks_destroyed(
            miner, tuple((210 + swing, 300, z) for z in (40, 41, 42)), True
        ))
    assert not mode.ground_diamonds
    asyncio.run(mode.on_blocks_destroyed(
        miner, tuple((400, 300, z) for z in (40, 41, 42)), True
    ))
    assert len(mode.ground_diamonds) == 1


def test_no_second_guarantee_while_a_diamond_is_in_play(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    miner = _miner(server, mode, now)
    _mine(mode, miner, 300)
    assert len(mode.ground_diamonds) == 1

    now[0] += 30.0
    _mine(mode, miner, 400, start=1000)
    assert len(mode.ground_diamonds) + len(mode.carriers) == 1

    # It expires unclaimed; the blocks mined meanwhile already count.
    now[0] += 120.0
    asyncio.run(mode.on_tick(1))
    assert not mode.ground_diamonds
    _mine(mode, miner, 1, start=5000)
    assert len(mode.ground_diamonds) == 1


def test_only_mined_blocks_of_live_players_count(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    miner = _miner(server, mode, now)

    for index in range(400):   # explosions
        asyncio.run(mode.on_blocks_destroyed(miner, ((210, 300 + index, 40),), False))
    miner.alive = False
    _mine(mode, miner, 400)
    assert not mode.ground_diamonds and mode._mined_since_discovery == 0


def test_round_restart_starts_the_count_again(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    miner = _miner(server, mode, now)
    _mine(mode, miner, 250)

    asyncio.run(mode.on_mode_start())
    mode._rng = NEVER

    assert mode._mined_since_discovery == 0
    _mine(mode, miner, 299)
    assert not mode.ground_diamonds
    _mine(mode, miner, 1, start=299)
    assert len(mode.ground_diamonds) == 1


def test_zero_keeps_the_retail_chance_only(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now, settings={
        "score_limit": 5, "max_active_bases": 1, "max_active_diamonds": 2,
        "discovery_guarantee_blocks": 0,
    })
    miner = _miner(server, mode, now)

    _mine(mode, miner, 2000)

    assert not mode.ground_diamonds


def test_shipped_config_documents_the_key():
    with (ROOT / "config.toml").open("rb") as stream:
        table = tomllib.load(stream)["modes"]["dia"]
    assert table["discovery_guarantee_blocks"] == DEFAULT_DISCOVERY_GUARANTEE_BLOCKS
    assert table["loose_cash_in"] is True


# -- every Diamond Mine map, real terrain, real digging -----------------------


class _Connection:
    def __init__(self, server):
        self.server = server
        self.player = None
        self.in_game = True
        self.known_entity_ids = set()
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30, **_kwargs):
        self.sent.append(bytes(data))


def _join(server, player_id, team, position):
    connection = _Connection(server)
    player = Player(player_id, f"Digger{player_id}", team, C.RIFLE_TOOL, connection)
    connection.player = player
    player.spawn(*position)
    player.set_tool(C.SPADE_TOOL)
    server.players[player_id] = player
    server.connections[player_id] = connection
    return player, connection


async def _drain(server, mode):
    while server._mode_events:
        name, args = server._mode_events.popleft()
        handler = getattr(mode, name, None)
        if handler is not None:
            await handler(*args)
    await mode.on_tick(server.loop_count)


async def _dig_until_found(server, mode, player, origin, limit=400):
    """Walk from ``origin`` toward the middle of the map, spading the ground."""
    wm = server.world_manager
    handler = PacketHandler(server)
    team = server.teams[int(player.team)]
    score = team.score
    heading = math.atan2(256.0 - origin[1], 256.0 - origin[0])
    ahead = (math.cos(heading), math.sin(heading))
    side = (-ahead[1], ahead[0])
    mined = 0
    for swing in range(limit):
        if swing % 3 == 0:
            step, row = 1 + (swing // 3) % 60, (swing // 3) // 60
            x = origin[0] + ahead[0] * step + side[0] * 3 * row
            y = origin[1] + ahead[1] * step + side[1] * 3 * row
            player.spawn(*wm.dry_ground_anchor(
                min(500.0, max(11.0, x)), min(500.0, max(11.0, y))
            ))
            player.set_tool(C.SPADE_TOOL)
        # This map/discovery fixture deliberately removes swing pacing so it
        # can exercise hundreds of real dig packets without a live timer.
        # Reset the shared admission lane as well as the legacy deadline.
        # Cadence itself is covered by the packet-bunching regression suite.
        player.next_shot_time = 0.0
        action_clock.reset(player, "fire")
        tx = int(player.x + ahead[0] * 1.4)
        ty = int(player.y + ahead[1] * 1.4)
        target = (tx + 0.5, ty + 0.5, wm.map.get_z(tx, ty) + 0.5)
        eye = player.eye
        aim = [target[i] - eye[i] for i in range(3)]
        length = math.sqrt(sum(value * value for value in aim))
        aim = [value / length for value in aim]
        player.set_orientation_vector(*aim)
        packet = ShootPacket()
        packet.loop_count = server.loop_count
        packet.shooter_id = player.id
        packet.shot_on_world_update = 1
        packet.x, packet.y, packet.z = eye
        packet.ori_x, packet.ori_y, packet.ori_z = aim
        packet.damage = packet.penetration = packet.secondary = 0
        packet.seed = swing & 0xFF
        before = len(server._mode_events)
        await handler.handle(player, bytes(packet.generate()))
        mined += sum(
            len(args[1]) for name, args in list(server._mode_events)[before:]
            if name == "on_blocks_destroyed" and args[2]
        )
        await _drain(server, mode)
        if mode.ground_diamonds or mode.carriers or team.score != score:
            return mined
    return mined


async def _round(server, mode, player, connection, origin):
    wm = server.world_manager
    connection.sent.clear()
    team = server.teams[int(player.team)]
    score = team.score
    mined = await _dig_until_found(server, mode, player, origin)
    found = mode.ground_diamonds or mode.carriers or team.score != score
    assert found, f"no diamond after {mined} blocks"
    assert mined <= DEFAULT_DISCOVERY_GUARANTEE_BLOCKS + 3

    shown = [
        CreateEntity(ByteReader(data[1:])).entity
        for data in connection.sent if data[0] == CreateEntity.id
    ]
    diamonds = [entity for entity in shown if entity.type == int(C.DIAMOND_PICKUP)]
    assert len(diamonds) == 1
    assert diamonds[0].fuse == pytest.approx(mode.diamond_lifetime, abs=0.1)
    # Reachable: it appeared in the hole the digger had just made.
    cell = tuple(
        int(math.floor(value))
        for value in (diamonds[0].pos_x, diamonds[0].pos_y, diamonds[0].pos_z)
    )
    assert not wm.get_solid(*cell)
    lines = [
        LocalisedMessage(ByteReader(data[1:])).string_id
        for data in connection.sent if data[0] == LocalisedMessage.id
    ]
    assert "DIAMOND_UNCOVERED" in lines

    if mode.ground_diamonds:
        # The digger picks it up by walking onto it.
        diamond = next(iter(mode.ground_diamonds.values()))
        assert math.dist(diamond.position, player.eye) <= 6.0
        player.set_position(diamond.position[0], diamond.position[1],
                            diamond.position[2] - 1.0)
        await _drain(server, mode)
    if team.score == score:
        assert player.id in mode.carriers
        # Cashing it in at the open drop-off scores.
        player.set_position(*mode.active_dropoffs[0].zone.center)
        await _drain(server, mode)
    # (A diamond dug up inside the drop-off is cashed in on the spot.)
    assert any(data[0] == PickPickup.id for data in connection.sent)
    assert team.score == score + 1
    assert not mode.carriers and not mode.ground_diamonds


async def _play(name, follow_up):
    config = ServerConfig(default_mode="dia", default_map=name)
    server = BattleSpadesServer(config)
    assert server.world_manager.load_map(name)
    mode = server.mode = DiamondMineMode(server)
    await mode.on_mode_start()
    mode._rng = NEVER
    assert mode.active_dropoffs, "no open drop-off"
    wm = server.world_manager
    origin = wm.dry_ground_anchor(*wm.team_base_anchor(TEAM1)[:2])
    player, connection = _join(server, 1, TEAM1, origin)

    await _round(server, mode, player, connection, origin)

    # The same map again, in place.
    await mode.on_mode_start()
    mode._rng = NEVER
    other = wm.dry_ground_anchor(*wm.team_base_anchor(TEAM2)[:2])
    await _round(server, mode, player, connection, other)

    # And across a map change.
    await mode.deactivate()
    assert server.world_manager.load_map(follow_up)
    server.entity_registry.clear()
    connection.known_entity_ids.clear()   # Connection.reset_for_scene_reload
    mode = server.mode = DiamondMineMode(server)
    await mode.on_mode_start()
    mode._rng = NEVER
    wm = server.world_manager
    origin = wm.dry_ground_anchor(*wm.team_base_anchor(TEAM1)[:2])
    await _round(server, mode, player, connection, origin)


@pytest.mark.parametrize("name", _rotation())
def test_every_diamond_map_yields_a_reachable_diamond(name):
    rotation = _rotation()
    follow_up = rotation[(rotation.index(name) + 1) % len(rotation)]
    asyncio.run(_play(name, follow_up))


def test_rotation_is_the_sixteen_official_maps():
    assert len(_rotation()) == 16
