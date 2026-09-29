"""Retail end-of-round scoreboard (packets 73/72) and mode team-lock updates (79/80).

IDA on the stock gameScene.pyd: ShowTextMessage(73) -> GameScene.show_text_message
(message_id, duration) sets the HUD scoreboard headline; ForceShowScores(72) ->
GameScene.force_show_scores(forced); LockTeam(79)/TeamLockClass(80) update
teams[id].locked / locked_class. None of them changes scenes.
"""

from __future__ import annotations

import asyncio

import shared.constants as C

from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombiePhase
from server.game_constants import TEAM1, TEAM2
from shared.bytes import ByteReader
from shared.packet import ForceShowScores, LockTeam, ShowTextMessage, TeamLockClass
from tests import test_end_sequence as seq
from tests import test_zombie as zombie_fixture


def _packets(rows, packet_type):
    return [packet_type(ByteReader(data[1:])) for data in rows if data and data[0] == packet_type.id]


def _run_end(monkeypatch, winner, *, enabled=True):
    monkeypatch.setattr(asyncio, "sleep", seq._fast_sleep)

    async def scenario():
        server = seq._Server()
        server.config = type("Cfg", (), {})()
        server.config.end_screen_seconds = 9.0
        server.config.end_round_scoreboard = enabled
        server.config.end_round_headline = enabled
        mode = seq._Mode(server)
        await mode.on_mode_start()
        server.broadcast_packets.clear()
        await mode.on_mode_end(winner=winner)
        for _ in range(8):
            await seq._REAL_SLEEP(0)
        return server

    return asyncio.run(scenario())


def test_round_end_holds_scores_until_the_in_place_restart(monkeypatch):
    server = _run_end(monkeypatch, 0)
    ids = [data[0] for data in server.broadcast_packets if data]
    forced = [p.forced for p in _packets(server.broadcast_packets, ForceShowScores)]
    assert forced == [1, 0], "held open at the win, released by the in-place restart"
    # The hold comes before the stats data; the release comes after the
    # restart began. The headline (73) belongs to the ViewGameStats screen
    # of a map rollover, so a same-map restart never sends it, and the
    # terminal packets are still never used.
    assert ids.index(ForceShowScores.id) < ids.index(67)
    assert not _packets(server.broadcast_packets, ShowTextMessage)
    assert 53 not in ids and 52 not in ids


def test_round_end_disabled_flag_sends_no_hold(monkeypatch):
    server = _run_end(monkeypatch, 0, enabled=False)
    assert not _packets(server.broadcast_packets, ShowTextMessage)
    assert not _packets(server.broadcast_packets, ForceShowScores)


def test_rollover_headline_follows_show_game_stats(monkeypatch):
    from tests import test_match_transitions as mt

    server = mt._Server()
    service = mt.MatchTransitionService(server)
    candidate = mt.SimpleNamespace(map_name="HallwayPin", config=None)

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(service, "_load_world_candidate", lambda *_args: candidate)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: mt._NewMode)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    result = asyncio.run(service.change_map_after_end_screen(
        "HallwayPin", end_screen_seconds=9.5,
        headline_message_id=int(C.ZOMBIE_WIN_MESSAGE),
    ))
    assert result.ok is True
    ids = [packet[0] for packet in server.broadcast_packets if packet]
    assert ids.index(53) < ids.index(ShowTextMessage.id) < ids.index(52)
    headline = _packets(server.broadcast_packets, ShowTextMessage)
    assert [(p.message_id, round(p.duration, 2)) for p in headline] == [(int(C.ZOMBIE_WIN_MESSAGE), 9.5)]


def test_rollover_releases_the_forced_scoreboard_before_show_game_stats(monkeypatch):
    """ForceShowScores(1) locks the client to ViewScores (manager.
    locked_to_scene). Live 2026-09-26: packet 53 sent under that lock left
    the plain scoreboard up, so the stats screen, its headline and its music
    never appeared. The release (72 forced=0) must directly precede 53."""
    from tests import test_match_transitions as mt

    server = mt._Server()
    service = mt.MatchTransitionService(server)
    candidate = mt.SimpleNamespace(map_name="HallwayPin", config=None)

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(service, "_load_world_candidate", lambda *_args: candidate)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: mt._NewMode)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    result = asyncio.run(service.change_map_after_end_screen(
        "HallwayPin", end_screen_seconds=9.5,
        headline_message_id=int(C.TEAM_SCORES_MESSAGE),
    ))
    assert result.ok is True
    rows = [packet for packet in server.broadcast_packets if packet]
    ids = [packet[0] for packet in rows]
    stats = ids.index(53)
    assert ids[stats - 1] == ForceShowScores.id
    assert ForceShowScores(ByteReader(rows[stats - 1][1:])).forced == 0
    assert stats < ids.index(ShowTextMessage.id) < ids.index(52)


def test_mode_specific_headlines():
    from modes.demolition import DemolitionMode
    from modes.occupation import OccupationMode
    from modes.vip import VIPMode

    assert ZombieHeadline().end_message_id(SURVIVOR_TEAM) == int(C.SURVIVOR_WIN_MESSAGE)
    assert ZombieHeadline().end_message_id(ZOMBIE_TEAM) == int(C.ZOMBIE_WIN_MESSAGE)
    assert VIPMode.end_message_id(None, TEAM1) == int(C.VIP_TEAM1_WIN_MESSAGE)
    assert VIPMode.end_message_id(None, TEAM2) == int(C.VIP_TEAM2_WIN_MESSAGE)
    assert VIPMode.end_message_id(None, None) == int(C.TEAM_SCORES_DRAW)
    assert OccupationMode.end_message_id(None, TEAM1) == int(C.OCCUPATION_WIN_MESSAGE)
    assert DemolitionMode.end_message_id(None, TEAM2) == int(C.DEMOLITION_END_MESSAGE)
    assert DemolitionMode.end_message_id(None, None) == int(C.TEAM_SCORES_DRAW)


def test_score_headline_names_a_forced_winner_explicitly():
    from types import SimpleNamespace

    from modes.base_mode import BaseMode

    def headline(score1, score2, winner):
        mode = SimpleNamespace(server=SimpleNamespace(teams={
            TEAM1: SimpleNamespace(score=score1),
            TEAM2: SimpleNamespace(score=score2),
        }))
        return BaseMode.end_message_id(mode, winner)

    # The score leader won: the client derives the name from the scores.
    assert headline(10, 3, TEAM1) == int(C.TEAM_SCORES_MESSAGE)
    assert headline(3, 10, TEAM2) == int(C.TEAM_SCORES_MESSAGE)
    # A forced winner behind (or level) on score is named by id.
    assert headline(3, 10, TEAM1) == int(C.VIP_TEAM1_WIN_MESSAGE)
    assert headline(10, 3, TEAM2) == int(C.VIP_TEAM2_WIN_MESSAGE)
    assert headline(5, 5, TEAM2) == int(C.VIP_TEAM2_WIN_MESSAGE)
    assert headline(5, 5, None) == int(C.TEAM_SCORES_DRAW)


class ZombieHeadline:
    """Call the Zombie override without building a whole mode."""

    def end_message_id(self, winner):
        from modes.zombie import ZombieMode

        return ZombieMode.end_message_id(None, winner)


def test_zombie_outbreak_publishes_team_and_class_locks_and_locks_survivor_class():
    server, mode = zombie_fixture._new_mode(player_count=3)
    server.packets.clear()
    asyncio.run(mode.on_tick(1))
    assert mode.phase is ZombiePhase.ACTIVE
    locks = [(p.team_id, p.locked) for p in _packets(server.packets, LockTeam)]
    assert locks[-2:] == [(ZOMBIE_TEAM, 0), (SURVIVOR_TEAM, 1)]
    class_locks = [(p.team_id, p.locked) for p in _packets(server.packets, TeamLockClass)]
    assert class_locks[-1] == (SURVIVOR_TEAM, 1)

    from server.builders.state_data import build_state_data
    state = build_state_data(server, player_id=3)
    assert state.team2_locked_class is True

    survivor = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    from server.class_selection import normalize_class_selection
    same = normalize_class_selection(int(survivor.class_id), [], [], [])
    from modes.zombie import _SURVIVOR_CLASSES
    other = next(c for c in sorted(_SURVIVOR_CLASSES) if c != int(survivor.class_id))
    changed = normalize_class_selection(other, [], [], [])
    assert mode.allows_class_selection(survivor, same)
    assert not mode.allows_class_selection(survivor, changed)

    # A new round unlocks the survivors again.
    server.packets.clear()
    asyncio.run(mode.on_mode_start())
    class_locks = [(p.team_id, p.locked) for p in _packets(server.packets, TeamLockClass)]
    assert class_locks[-1] == (SURVIVOR_TEAM, 0)
    assert build_state_data(server, player_id=3).team2_locked_class is False
