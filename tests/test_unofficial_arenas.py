"""Production arena lifecycle and post-load objective onboarding."""
import asyncio

import pytest
from shared.bytes import ByteReader
from shared.packet import ChatMessage
from server.game_constants import CHAT_SYSTEM, TEAM1
from server.join_greeting import send_join_greeting
from modes.unofficial.catalog import SPECS
from modes.unofficial.guides import GUIDES
from tests.test_unofficial_modes import add_player, make_game, start
from tests.test_unofficial_objectives import drain


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.code)
def test_production_arena_starts_with_clear_spawns_and_reachable_instructions(spec):
    async def scenario():
        server, mode, blue, green, _ = make_game(spec.code, map_name=spec.map_name)
        await start(mode)
        assert mode.phase == "live"
        assert server.world_manager.spawn_position_is_safe(blue.position)
        assert server.world_manager.spawn_position_is_safe(green.position)
        for position in mode.data.get("spawn_points", ()):
            assert server.world_manager.spawn_position_is_safe(position), position
        assert spec.code in GUIDES
        assert mode.data["design_version"] == 3
        await mode.show_rules(blue)
        messages = [ChatMessage(ByteReader(raw[1:])).value for raw in blue.connection.sent
                    if raw[0] == ChatMessage.id]
        assert any("/funrules" in line for line in messages)
        await mode.deactivate()
    asyncio.run(scenario())


def test_false_wall_rules_arrive_after_world_reveal_for_every_new_human_once():
    async def scenario():
        server, mode, _, _, _ = make_game("false_wall")
        await start(mode)
        for pid in (6, 7):
            player = add_player(server, pid, TEAM1)
            player.connection.in_game = False
            server.respawn_player(player)
            await mode.on_player_join(player)
            await drain(server)
            assert player not in mode._notified
            player.connection.sent.clear()
            player.connection.in_game = True
            send_join_greeting(player.connection)
            await drain(server)
            messages = [ChatMessage(ByteReader(raw[1:])) for raw in player.connection.sent
                        if raw[0] == ChatMessage.id]
            lines = [p.value for p in messages if p.chat_type == CHAT_SYSTEM]
            assert any("/stash" in line for line in lines)
            assert any("First delivery wins" in line for line in lines)
            assert all(len(line.encode("utf-8")) <= 90 for line in lines)
            count = len(player.connection.sent)
            send_join_greeting(player.connection)
            await mode.on_player_ready(player)
            assert len(player.connection.sent) == count
    asyncio.run(scenario())


def test_false_wall_default_time_and_human_can_replace_bot_stash_once():
    async def scenario():
        server, mode, blue, _, _ = make_game("false_wall")
        assert mode.duration == 300 and mode.spec.preparation == 180
        mode.preparation = 180
        await mode.on_mode_start()
        await mode.on_tick(1)
        bot = add_player(server, 6, TEAM1, bot=True)
        server.respawn_player(bot)
        await mode.on_player_join(bot)
        await mode.stash(bot)
        assert mode.stash_owners[TEAM1] is bot
        await mode.stash(blue)
        assert mode.stash_owners[TEAM1] is blue
        token = next(c for c in mode.cargo if c.team == TEAM1)
        point = token.home
        blue.position = (blue.x, blue.y + 2, blue.z)
        await mode.stash(blue)
        assert token.home == point
    asyncio.run(scenario())


def test_tower_has_redundant_supports_but_core_falls_after_all_four_are_cut():
    server, _, _, _, _ = make_game("towerfall", map_name="TwinBellFoundry")
    world = server.world_manager
    cuts = [(216 + dx, 256 + dy, 90) for dx in (-4, 4) for dy in (-4, 4)]
    for i, cell in enumerate(cuts):
        world.map.remove_point(*cell)
        chunks = world.find_unsupported_chunks([cell])
        if i < 3:
            assert not chunks
        else:
            assert any((216, 256, 75) in chunk for chunk in chunks)
