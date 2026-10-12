"""Bots must join and respawn with the same class roster as human players."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import shared.constants as C
from modes import get_mode_class
from modes.zombie import ZombiePhase
from server import hardcore as HC
from server.bot_ai.director import BotDirector
from server.class_data import BATTLESPADES_TEAM_CLASSES
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.game_rules import CLASS_RULE_IDS
from server.main import BattleSpadesServer
from shared.bytes import ByteReader
from shared.packet import CreatePlayer


def _game(mode_code: str):
    server = BattleSpadesServer(ServerConfig(default_mode=mode_code))
    server.world_manager.generate_flat_map()
    server.mode = get_mode_class(mode_code)(server)
    director = BotDirector(server, supervisor=SimpleNamespace())
    server.bots = director
    broadcasts: list[bytes] = []
    server.broadcast = lambda data, **kwargs: broadcasts.append(bytes(data))
    return server, director, broadcasts


@pytest.mark.parametrize("team", [TEAM1, TEAM2])
@pytest.mark.parametrize("requested", [
    None, C.CLASS_SOLDIER, C.CLASS_GANGSTER_1, C.CLASS_GANGSTER_VIP_1,
])
def test_tc_bot_join_advertises_gangster_with_matching_equipment(team, requested):
    server, director, broadcasts = _game("tc")
    bot = asyncio.run(director.add_bot(team=team, class_id=requested))

    assert bot is not None
    assert bot.class_id in C.MAFIA_TEAM_CLASSES
    if requested in C.MAFIA_TEAM_CLASSES:
        assert bot.class_id == requested
    assert bot.tool in bot.loadout
    packets = [CreatePlayer(ByteReader(data[1:]))
               for data in broadcasts if data[0] == CreatePlayer.id]
    assert len(packets) == 1
    assert packets[0].class_id == bot.class_id
    assert tuple(packets[0].loadout) == tuple(bot.loadout)
    assert tuple(packets[0].prefabs) == tuple(bot.prefabs)


def test_tc_bot_class_rotation_stays_gangster_through_respawn():
    server, director, _ = _game("tc")
    bot = asyncio.run(director.add_bot(team=TEAM1, class_id=C.CLASS_GANGSTER_1))
    runtime = director._runtime[bot.id]
    chosen = set()
    for _ in range(32):
        runtime.was_alive = True
        runtime.class_lives_remaining = 1
        bot.alive = False
        director._update_class_lifetime(runtime)
        assert bot.pending_selection is not None
        assert bot.pending_selection.class_id in C.MAFIA_TEAM_CLASSES
        server.round_lifecycle.respawn_player(bot)
        chosen.add(bot.class_id)
        assert bot.class_id in C.MAFIA_TEAM_CLASSES
    assert chosen == set(C.MAFIA_TEAM_CLASSES)


@pytest.mark.parametrize("mode,expected", [
    ("tc", C.MAFIA_TEAM_CLASSES),
    ("territory_control", C.MAFIA_TEAM_CLASSES),
    ("vip", C.MAFIA_TEAM_CLASSES),
    ("cctf", C.CLASSIC_TEAM_CLASSES),
    ("tdm", BATTLESPADES_TEAM_CLASSES),
    ("ctf", C.DEFAULT_TEAM_CLASSES),
    ("hc_tc", HC.CLASS_IDS),
])
def test_bot_class_candidates_follow_current_mode(mode, expected):
    _, director, _ = _game(mode)
    chosen = {director._choose_class(TEAM1) for _ in range(128)}
    assert chosen == set(expected)


def test_live_tc_mode_wins_over_previous_configured_mode():
    server, director, _ = _game("tc")
    server.config.default_mode = "tdm"
    assert director._choose_class(TEAM1) in C.MAFIA_TEAM_CLASSES


@pytest.mark.parametrize("mode", ["zom", "hc_zom"])
def test_zombie_bot_classes_still_follow_the_assigned_team(mode):
    async def scenario():
        server, director, _ = _game(mode)
        survivor = await director.add_bot(team=TEAM2)
        assert survivor is not None
        assert survivor.team == TEAM2
        expected = HC.CLASS_IDS if mode == "hc_zom" else BATTLESPADES_TEAM_CLASSES
        assert survivor.class_id in expected
        server.mode.phase = ZombiePhase.ACTIVE
        infected = await director.add_bot(team=TEAM1)
        assert infected is not None
        assert infected.team == TEAM1
        assert infected.class_id == C.CLASS_ZOMBIE
        assert C.ZOMBIEHAND_TOOL in infected.loadout
    asyncio.run(scenario())


def test_reconnected_bot_roster_uses_new_mode_classes():
    async def scenario():
        server, director, _ = _game("tdm")
        director._started = True
        for _ in range(4):
            assert await director.add_bot() is not None
        await director.prepare_for_game_transition()
        server.config.default_mode = "tc"
        server.mode = get_mode_class("tc")(server)
        await director._reconnect_roster()
        assert len(director.bots) == 4
        assert all(bot.class_id in C.MAFIA_TEAM_CLASSES for bot in director.bots)
    asyncio.run(scenario())


def test_disabled_requested_bot_class_cannot_bypass_operator_rules():
    server, director, _ = _game("tdm")
    server.config.game_rules.apply({"RULE_ENABLE_CLASS_ENGINEER": False})
    bot = asyncio.run(director.add_bot(team=TEAM1, class_id=C.CLASS_ENGINEER))
    assert bot is not None
    assert bot.class_id != C.CLASS_ENGINEER


def test_no_enabled_bot_classes_does_not_fall_back_to_disabled_soldier():
    server, director, broadcasts = _game("tdm")
    server.config.game_rules.apply({key: False for key in CLASS_RULE_IDS})
    bot = asyncio.run(director.add_bot(team=TEAM1))
    assert bot is None
    assert not director.bots
    assert not server.players
    assert not any(data[0] == CreatePlayer.id for data in broadcasts)
