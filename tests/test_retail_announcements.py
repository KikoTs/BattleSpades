"""Mode announcements use the retail string-table ids, team-relative like the original.

Evidence: every id asserted here exists in the client's string table but is
referenced by no client binary (gameScene, hud, gamemanager, player, aos.pkg),
so the original server had to send it as LocalisedMessage(50). See
docs/ANNOUNCEMENTS_RETAIL_2026-09-24.md.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C

from modes.ctf import CTFMode
from modes.tdm import TDMMode
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombiePhase
from server.game_constants import TEAM1, TEAM2
from shared.bytes import ByteReader
from shared.packet import ChatMessage, LocalisedMessage
from tests import test_ctf_entities as ctf_fixture
from tests import test_vip as vip_fixture
from tests import test_zombie as zombie_fixture


def _localised(rows):
    return [
        LocalisedMessage(ByteReader(data[1:]))
        for data in rows
        if data and data[0] == LocalisedMessage.id
    ]


def _ids(rows):
    return [packet.string_id for packet in _localised(rows)]


def _freeform(rows):
    return [
        ChatMessage(ByteReader(data[1:])).value
        for data in rows
        if data and data[0] == ChatMessage.id
    ]


def test_zombie_round_uses_retail_outbreak_last_man_and_win_cues():
    server, mode = zombie_fixture._new_mode(player_count=3)
    assert mode.phase is ZombiePhase.COUNTDOWN
    assert "ZOMBIE_VIRUS_RELEASED" in _ids(server.packets)
    assert _freeform(server.packets) == []

    asyncio.run(mode.on_tick(1))
    assert mode.phase is ZombiePhase.ACTIVE
    zombies = [p for p in server.players.values() if p.team == ZOMBIE_TEAM]
    survivors = [p for p in server.players.values() if p.team == SURVIVOR_TEAM]
    assert len(zombies) == 1 and len(survivors) == 2
    # Every player was a survivor when the outbreak clock armed.
    assert _ids(zombies[0].connection.sent) == [
        "ZOMBIE_START_SURVIVOR", "YOU_HAVE_BEEN_INFECTED"
    ]
    for survivor in survivors:
        assert "ZOMBIE_INFECTION_DETECTED" in _ids(survivor.connection.sent)
        assert "YOU_HAVE_BEEN_INFECTED" not in _ids(survivor.connection.sent)
    # Retail never names Patient Zero in free text.
    assert _freeform(server.packets) == []

    # One survivor dies: everyone sees LAST_MAN_STANDING, the human gets the
    # personal line, and the newly infected body gets the infection line.
    victim, last = survivors
    victim.die()
    asyncio.run(mode.on_player_death(victim, zombies[0], 0))
    assert "LAST_MAN_STANDING" in _ids(server.packets)
    assert _ids(last.connection.sent)[-1] == "ZOMBIE_LAST_MAN"
    assert "YOU_HAVE_BEEN_INFECTED" in _ids(victim.connection.sent)

    winners = []

    async def finish(winner, string_id):
        winners.append((winner, string_id))

    mode._finish_round = finish
    asyncio.run(mode._end_by_time())
    assert winners == [(SURVIVOR_TEAM, "SURVIVOR_WIN")]


def test_zombie_finish_round_broadcasts_the_retail_win_id():
    server, mode = zombie_fixture._new_mode(player_count=2)
    sent = []

    async def end(winner):
        sent.append(winner)

    mode.on_mode_end = end
    mode.score_limit = 1
    asyncio.run(mode._finish_round(ZOMBIE_TEAM, "ZOMBIE_WIN"))
    assert _ids(server.packets)[-1] == "ZOMBIE_WIN"
    assert sent == [ZOMBIE_TEAM]


def _vip_mode(player_count=2):
    server = vip_fixture._Server()
    players = {}
    for player_id in range(1, player_count + 1):
        team = TEAM1 if player_id % 2 else TEAM2
        players[player_id] = vip_fixture._player(server, player_id, team)
    from modes.vip import VIPMode

    mode = VIPMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    return server, mode, players


def test_vip_selection_and_kill_cues_are_team_relative_retail_ids():
    server, mode, players = _vip_mode(4)
    asyncio.run(mode.on_tick(1))
    if mode.phase is not vip_fixture.VIPPhase.ACTIVE:
        asyncio.run(mode._select_vips())
    assert mode.phase is vip_fixture.VIPPhase.ACTIVE
    ids = _ids(server.packets)
    assert "VIP_AWAITING_CHOICE" in ids and "VIP_START" in ids
    assert _freeform(server.packets) == []
    for team in (TEAM1, TEAM2):
        vip = mode.vips[team]
        assert "VIP_NAME_IS_VIP" not in _ids(vip.connection.sent)
        mates = [p for p in players.values() if p.team == team and p is not vip]
        for mate in mates:
            packet = _localised(mate.connection.sent)[-1]
            assert packet.string_id == "VIP_NAME_IS_VIP"
            assert packet.parameters == [vip.name]

    vip = mode.vips[TEAM1]
    killer = next(p for p in players.values() if p.team == TEAM2 and p is not mode.vips[TEAM2])
    vip.alive = False
    vip.spawned = False
    asyncio.run(mode.on_player_death(vip, killer, 0))
    for player in players.values():
        expected = "VIP_KILLED_VIP_YOURTEAM" if player.team == TEAM1 else "VIP_KILLED_VIP_OPPOSITION"
        assert expected in _ids(player.connection.sent)
    assert _freeform(server.packets) == []


def test_vip_round_end_uses_team_defeat_or_draw():
    server, mode, players = _vip_mode(4)
    asyncio.run(mode.on_tick(1))
    if mode.phase is not vip_fixture.VIPPhase.ACTIVE:
        asyncio.run(mode._select_vips())
    mode.score_limit = 99
    asyncio.run(mode._finish_round(TEAM2))
    packet = _localised(server.packets)[-1]
    assert packet.string_id == "TEAM_DEFEAT"
    assert packet.parameters == [server.teams[TEAM2].name]
    assert packet.localise_parameters == 1

    server2, mode2, _players = _vip_mode(4)
    asyncio.run(mode2.on_tick(1))
    if mode2.phase is not vip_fixture.VIPPhase.ACTIVE:
        asyncio.run(mode2._select_vips())
    asyncio.run(mode2._finish_round(None))
    assert _ids(server2.packets)[-1] == "GAME_DRAWN"


def _ctf_player(server, player_id, team):
    connection = ctf_fixture._Connection()
    player = SimpleNamespace(
        id=player_id, name=f"P{player_id}", team=team, alive=True, spawned=True,
        score=0, connection=connection, position=(100.0, 100.0, 50.0),
        x=100.0, y=100.0, z=50.0,
    )
    connection.player = player
    server.players[player_id] = player
    return player


def test_ctf_intel_cues_are_the_retail_carrier_team_and_owner_trio():
    server = ctf_fixture._Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    carrier = _ctf_player(server, 1, TEAM1)
    mate = _ctf_player(server, 2, TEAM1)
    owner = _ctf_player(server, 3, TEAM2)
    for team in server.teams.values():
        team.players = []
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    assert _ids(carrier.connection.sent) == ["CTF_YOU_HAVE_FLAG"]
    packet = _localised(mate.connection.sent)[-1]
    assert packet.string_id == "CTF_TEAM_HAS_FLAG" and packet.parameters == ["P1"]
    packet = _localised(owner.connection.sent)[-1]
    assert packet.string_id == "CTF_ENEMY_HAS_FLAG" and packet.parameters == ["P1"]
    assert _freeform(server.packets) == []

    asyncio.run(mode._return_intel(TEAM2, returned_by=owner))
    packet = _localised(server.packets)[-1]
    assert packet.string_id == "CTF_FLAG_RETURNED"
    assert packet.parameters == ["P3", server.teams[TEAM2].name]
    assert packet.localise_parameters == 1


def test_tdm_reveals_the_retail_start_cue_and_never_announces_leads():
    server = vip_fixture._Server()
    server.config.default_mode = "tdm"
    mode = TDMMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    connection = vip_fixture._Connection()
    mode.reveal_to(connection)
    assert _ids(connection.sent) == ["TEAM_DEATHMATCH_START"]
    server.teams[TEAM1].score = 5
    asyncio.run(mode.on_tick(60 * server.tick_rate))
    assert _freeform(server.packets) == []


def test_countdown_cues_fire_once_inside_their_window():
    server = vip_fixture._Server()
    server.config.default_mode = "tdm"
    mode = TDMMode(server)
    asyncio.run(mode.on_mode_start())
    mode.time_limit = 600.0
    cues = []
    mode.announce_localised = lambda string_id, parameters=(), **_kw: cues.append((string_id, tuple(parameters)))

    for remaining in (600.0, 301.0, 299.0, 298.0, 121.0, 118.0, 61.0, 59.0, 31.0, 28.0, 11.0, 9.0):
        mode._announce_countdown(remaining)
    assert cues == [
        ("COUNTDOWN_MINUTES", ("5",)),
        ("COUNTDOWN_MINUTES", ("2",)),
        ("ONE_MINUTE_LEFT", ()),
        ("COUNTDOWN_SECONDS", ("30",)),
        ("COUNTDOWN_SECONDS", ("10",)),
        ("COUNTDOWN_FROM_TEN", ("9",)),
    ]

    # A stall that skips a whole window stays quiet; a restarted clock re-arms.
    cues.clear()
    mode._countdown_fired.clear()
    mode._countdown_last_remaining = float("inf")
    mode._announce_countdown(400.0)
    mode._announce_countdown(50.0)
    assert cues == []
    mode._announce_countdown(599.0)
    mode._announce_countdown(60.0)
    assert cues == [("ONE_MINUTE_LEFT", ())]


def test_final_seconds_count_down_with_countdown_from_ten():
    server = vip_fixture._Server()
    server.config.default_mode = "tdm"
    mode = TDMMode(server)
    asyncio.run(mode.on_mode_start())
    cues = []
    mode.announce_localised = lambda string_id, parameters=(), **_kw: cues.append((string_id, tuple(parameters)))
    for tick in range(700, 0, -1):
        mode._announce_countdown(tick / 60.0)
    finals = [cue for cue in cues if cue[0] == "COUNTDOWN_FROM_TEN"]
    assert finals == [("COUNTDOWN_FROM_TEN", (str(n),)) for n in range(9, 0, -1)]
    # A stall that jumps from 8.5 s to 3.5 s skips 8..5 and shows only 4.
    cues.clear()
    mode._countdown_fired.clear()
    mode._countdown_last_remaining = float("inf")
    mode._announce_countdown(8.5)
    mode._announce_countdown(3.5)
    assert [cue[1][0] for cue in cues if cue[0] == "COUNTDOWN_FROM_TEN"] == ["9", "4"]
