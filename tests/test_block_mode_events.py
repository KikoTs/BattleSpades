"""Committed builds and explosive blasts reach the mode event queue.

Demolition's personal repair/destroy awards need to know which player
committed which cells; the queued hooks carry that.
"""

import asyncio
from types import SimpleNamespace

import pytest

from server.bot_ai.director import BotDirector
from server.config import ServerConfig
from server.main import BattleSpadesServer
from shared import constants as C


def _events(server, name):
    return [args for event, args in server._mode_events if event == name]


@pytest.mark.parametrize("line", [False, True])
def test_committed_builds_queue_on_blocks_built(line):
    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        server.world_manager.generate_flat_map()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        builder = await director.add_bot(team=2, class_id=int(C.CLASS_SOLDIER))
        builder.set_position(100.5, 100.5, 59.75)
        cells = ((103, 100, 61), (104, 100, 61)) if line else ((103, 100, 61),)
        reservation, reason = server.construction.reserve_construction(
            builder.id, 2, cells
        )
        assert reservation is not None, reason
        builder.blocks -= len(cells)
        server._mode_events.clear()
        if line:
            server.combat._commit_block_line(
                builder, 0, (*cells[0], *cells[-1]), cells, 0x123456
            )
        else:
            server.combat._commit_block_build(builder, 0, cells[0], 0x123456)
        built = _events(server, "on_blocks_built")
        assert built == [(builder, tuple(cells))]
        server.construction.release(reservation)

    asyncio.run(scenario())


def test_radius_blast_queues_on_blocks_destroyed_as_not_mined():
    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        server.world_manager.generate_flat_map()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        thrower = await director.add_bot(team=2, class_id=int(C.CLASS_SOLDIER))
        cells = ((120, 120, 62),)
        server._mode_events.clear()
        server.combat.broadcast_native_radius_destroy(
            thrower,
            cells[0],
            cells,
            damage=100.0,
            damage_type=0,
            causer_entity_id=1,
        )
        assert _events(server, "on_blocks_destroyed") == [(thrower, cells, False)]

    asyncio.run(scenario())
