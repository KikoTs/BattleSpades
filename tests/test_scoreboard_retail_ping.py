"""Scoreboard PING column: the WorldUpdate row ping field carries real values.

Retail gameScene.process_packet_world_update copies each row's signed short
ping into ``player.ping``; ingame_menus.draw_player_list prints
``str(player.ping)`` in the PING column.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

import server.replication as replication
from server.replication import (
    ReplicationService,
    WIRE_PING_MAX_MS,
    bot_ping_ms,
    wire_ping_ms,
)
from shared.bytes import ByteReader
from shared.packet import WorldUpdate

# id(1) + position/orientation/velocity (36) -> ping short at row +37.
_ROW_PING_OFFSET = 37
_HEADER = 7
_ROW = 56


class _Player:
    def __init__(self, pid, *, rtt=None, bot=False, name="P"):
        self.id = pid
        self.name = f"{name}{pid}"
        self.is_bot = bot
        self.alive = True
        self.spawned = True
        self.connection = (
            None if rtt is None
            else SimpleNamespace(peer=SimpleNamespace(roundTripTime=rtt))
        )

    def world_update_snapshot(self):
        return (
            (1.0, 2.0, 3.0), (0.0, 1.0, 0.0), (0.0, 0.0, 0.0),
            0, 5, 100, 0, 0, 0, 2, 0xFF, 0.0, 0.0, 0.0,
        )


def _service(players, loop_count=600):
    server = SimpleNamespace(
        players={player.id: player for player in players},
        loop_count=loop_count,
        entities={},
        rocket_turrets={},
        corpse_lifecycle=None,
    )
    return ReplicationService(server)


@pytest.fixture(autouse=True)
def _authorize_tools(monkeypatch):
    monkeypatch.setattr(replication, "equipped_tool_authorized", lambda *_: True)


def _wire_pings(data: bytes) -> dict[int, int]:
    count = struct.unpack_from("<H", data, 5)[0]
    pings = {}
    for index in range(count):
        row = _HEADER + index * _ROW
        pings[data[row]] = struct.unpack_from("<h", data, row + _ROW_PING_OFFSET)[0]
    return pings


def test_human_row_carries_enet_round_trip_in_milliseconds():
    service = _service([_Player(0, rtt=47), _Player(1, rtt=3.6)])

    data = service.build_world_update_data()

    assert _wire_pings(data) == {0: 47, 1: 4}
    # The shipped reader decodes the same value back into the row tuple.
    packet = WorldUpdate(ByteReader(data[1:]))
    assert packet.player_updates[0][3] == 47


def test_ping_is_clamped_to_the_scoreboard_column():
    service = _service([_Player(0, rtt=5000), _Player(1, rtt=-5), _Player(2)])

    pings = _wire_pings(service.build_world_update_data())

    assert pings == {0: WIRE_PING_MAX_MS, 1: 0, 2: 0}


def test_bot_ping_is_stable_plausible_and_drifts_slowly():
    bot = _Player(3, bot=True, name="Bot")

    samples = [bot_ping_ms(bot, loop) for loop in range(0, 60 * 120, 60)]

    assert all(1 <= value <= 90 for value in samples)
    assert max(samples) - min(samples) <= 6
    # Same bucket -> same value (no per-frame flicker on the scoreboard).
    assert bot_ping_ms(bot, 121) == bot_ping_ms(bot, 239)
    assert wire_ping_ms(bot, 500) == bot_ping_ms(bot, 500)


def test_bots_do_not_all_share_one_ping():
    bots = [_Player(pid, bot=True, name="Bot") for pid in range(12)]
    values = {bot_ping_ms(bot, 0) for bot in bots}
    assert len(values) > 3


def test_owner_row_keeps_its_ping_with_the_tool_sentinel():
    service = _service([_Player(0, rtt=21)])

    data = service.build_world_update_data(local_player_id=0)

    assert _wire_pings(data) == {0: 21}
    assert data[_HEADER + 48] == 0xFF
