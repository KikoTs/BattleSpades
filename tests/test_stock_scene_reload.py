"""Retained stock peers enter their loader on InitialInfo, without packet 110."""

import asyncio
from types import SimpleNamespace

import pytest
from server.config import ServerConfig
from server.connection import Connection
from server.main import BattleSpadesServer
from shared.packet import InitialInfo, MapDataValidation, MapSyncStart, MapSyncEnd, StateData


@pytest.mark.parametrize("answers_validation", [True, False])
def test_retained_stock_peer_requires_only_initialinfo_map_validation(answers_validation):
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    connection = Connection(SimpleNamespace(address=("127.0.0.1", 40997)), server)
    connection.map_sent = True
    connection.state_sent = True
    connection.in_game = True
    sent = []
    connection.send = lambda data, **_kwargs: sent.append(bytes(data))

    async def validation(packet_class, timeout):
        assert packet_class is MapDataValidation
        assert sent[0][0] == InitialInfo.id
        assert MapSyncStart.id not in [packet[0] for packet in sent]
        if not answers_validation:
            raise asyncio.TimeoutError
        return SimpleNamespace(crc=0)

    connection.wait_for = validation

    async def scenario():
        connection.arm_scene_transition()
        assert not connection._scene_transition_ready.is_set()
        return await connection.reload_scene()

    assert asyncio.run(scenario()) is answers_validation
    packet_ids = [packet[0] for packet in sent]
    if answers_validation:
        assert packet_ids.index(InitialInfo.id) < packet_ids.index(MapSyncStart.id)
        assert packet_ids.index(MapSyncStart.id) < packet_ids.index(MapSyncEnd.id)
        assert packet_ids.index(MapSyncEnd.id) < packet_ids.index(StateData.id)
        assert connection.map_sent and connection.state_sent
    else:
        assert packet_ids == [InitialInfo.id]
        assert not connection.map_sent and not connection.state_sent
