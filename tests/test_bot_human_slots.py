"""Bots never hold the slot a joining human needs."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from modes.tdm import TDMMode
from server.bot_ai.director import BotDirector
from server.config import ServerConfig
from server.main import BattleSpadesServer
from server.player import Player


async def _server_with_bots(*, max_players: int, bots: int, humans: int):
    config = ServerConfig()
    config.max_players = max_players
    config.bots.max_bots = bots
    config.bots.population_mode = "fixed"
    config.bots.reserve_human_slots = 1
    supervisor = SimpleNamespace(start=lambda _: None, discard_timeline=lambda: None,
                                 request_restart=lambda: None, close=lambda: None)
    server = BattleSpadesServer(config)
    server.world_manager.generate_flat_map()
    server.mode = TDMMode(server)
    await server.mode.on_mode_start()
    for index in range(humans):
        human = Player(index, f"Human{index}", 2, 6)
        server.players[human.id] = human
        server.teams[2].add_player(human)
    director = BotDirector(server, supervisor=supervisor)
    server.bots = director
    await director.start(initial_count=bots)
    return server, director


def test_fixed_population_always_leaves_one_slot_for_a_human():
    async def scenario():
        server, director = await _server_with_bots(max_players=4, bots=3, humans=1)
        try:
            director._next_population_at = 0.0
            await director._maintain_population(time.monotonic())
            assert len(director.bots) == 2
            assert server.get_next_player_id() >= 0
        finally:
            await director.close()
    asyncio.run(scenario())


def test_joining_player_takes_a_bot_slot_on_a_full_server():
    async def scenario():
        server, director = await _server_with_bots(max_players=4, bots=3, humans=1)
        try:
            assert server.get_next_player_id() == -1
            assert await director.make_room_for_human()
            assert len(director.bots) == 2
            assert server.get_next_player_id() >= 0
        finally:
            await director.close()
    asyncio.run(scenario())


def test_server_full_of_humans_has_no_bot_to_swap():
    async def scenario():
        server, director = await _server_with_bots(max_players=2, bots=0, humans=2)
        try:
            assert server.get_next_player_id() == -1
            assert not await director.make_room_for_human()
        finally:
            await director.close()
    asyncio.run(scenario())
