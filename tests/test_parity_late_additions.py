"""Late retail-parity additions (docs/PARITY_CHANGES_2026-09.md section 24).

Round-start cues to everyone, the retail mode lines the server never sent
(ZOMBIE_START_*, BASE_DEPLETED, BASE_OCCUPIED_*, DIAMOND_BASE), the Diamond
Mine map vote at 12 of 15, the Demolition build-phase clock, the Zombie
HeadCount type and the ``[audio] mode_start_music`` switch.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
import shared.constants_gamemode as CG

from modes.diamond_mine import DiamondMineMode
from modes.multi_hill import MultiHillMode
from modes.tdm import TDMMode
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL
from shared.bytes import ByteReader
from shared.packet import DisplayCountdown, LocalisedMessage, PlayMusic, StopMusic
from tests import test_multi_hill as mh_fixture
from tests import test_zombie as zombie_fixture
from tests.test_occupation_fixes import _mode as occupation_mode
from tests.test_recovered_objective_modes import _Player, _Server, _zone


def _ids(rows):
    return [
        LocalisedMessage(ByteReader(data[1:])).string_id
        for data in rows
        if data and data[0] == LocalisedMessage.id
    ]


class _Recorder:
    def __init__(self, in_game=True):
        self.sent = []
        self.in_game = in_game

    def send(self, data, **_kwargs):
        self.sent.append(bytes(data))


def test_tdm_start_cue_reaches_everyone_at_every_round_start():
    from tests.test_tdm import _TDMServer

    server = _TDMServer()
    playing = SimpleNamespace(id=1, team=TEAM1, connection=_Recorder())
    loading = SimpleNamespace(id=2, team=TEAM2, connection=_Recorder(in_game=False))
    bot = SimpleNamespace(id=3, team=TEAM2, connection=_Recorder(), is_bot=True)
    server.players = {1: playing, 2: loading, 3: bot}
    mode = TDMMode(server)
    server.mode = mode

    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_mode_start())  # in-place restart

    assert _ids(playing.connection.sent) == ["TEAM_DEATHMATCH_START"] * 2
    assert _ids(loading.connection.sent) == []  # gets it from reveal_to
    assert _ids(bot.connection.sent) == []
    late = _Recorder()
    mode.reveal_to(late)
    assert _ids(late.sent) == ["TEAM_DEATHMATCH_START"]


def test_multi_hill_timeout_announces_base_depleted(monkeypatch):
    now = [200.0]
    monkeypatch.setattr("modes.multi_hill.time.time", lambda: now[0])
    server = mh_fixture._Server()
    mode = MultiHillMode(server)
    asyncio.run(mode.on_mode_start())
    assert "BASE_DEPLETED" not in _ids(server.packets)

    now[0] = 200.0 + mode.base_active_time + 0.1
    asyncio.run(mode.on_tick(1))
    assert _ids(server.packets).count("BASE_DEPLETED") == 1
    assert mode.phase == "intermission"


def test_occupation_carrier_entering_base_shouts_base_occupied(monkeypatch):
    now = [300.0]
    server, mode = occupation_mode(monkeypatch, now)
    blue = _Player(1, TEAM1, (100.0, 100.0, 60.0))
    green = _Player(2, TEAM2, (0.0, 50.0, 60.0))
    for player in (blue, green):
        player.connection = player
        server.players[player.id] = player
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    assert blue.pickup_id == int(C.BOMB_PICKUP)
    assert "BASE_OCCUPIED_ATTACK" not in _ids(blue.sent)

    blue.set_position(mode.target_zone.center)
    now[0] = 300.5
    asyncio.run(mode.on_tick(2))
    assert _ids(blue.sent).count("BASE_OCCUPIED_ATTACK") == 1
    assert _ids(green.sent).count("BASE_OCCUPIED_DEFEND") == 1

    # Staying inside does not repeat the shout.
    now[0] = 310.0
    asyncio.run(mode.on_tick(3))
    assert _ids(blue.sent).count("BASE_OCCUPIED_ATTACK") == 1


def _diamond_mode(monkeypatch, now, score_limit=15):
    monkeypatch.setattr("modes.diamond_mine.time.time", lambda: now[0])
    server = _Server({
        "dia": {
            "score_limit": score_limit,
            "max_active_bases": 1,
            "max_active_diamonds": 2,
            "diamond_lifetime": 60,
        }
    })
    server.world_manager.map_metadata.diamond_base_zones.append(
        _zone(TEAM_NEUTRAL, 150)
    )
    server.world_manager.map_metadata.diamond_base_capacities.append(1)
    votes = []
    server.vote_manager = SimpleNamespace(ensure_map_vote=votes.append)
    mode = DiamondMineMode(server)
    mode._rng = SimpleNamespace(random=lambda: 0.0, randrange=lambda _n: 0)
    return server, mode, votes


def _cash_one(mode, player, now, tick):
    now[0] += 30.0  # past the retail diamond discovery spacing
    asyncio.run(mode.on_blocks_destroyed(player, ((120, 100, 50),), True))
    now[0] += 0.1
    asyncio.run(mode.on_tick(tick))
    assert player.id in mode.carriers
    player.set_position(mode.active_dropoffs[0].zone.center)
    now[0] += 0.1
    asyncio.run(mode.on_tick(tick + 1))
    assert player.id not in mode.carriers
    player.set_position((120.5, 100.5, 50.5))


def test_diamond_map_vote_opens_at_twelve_of_fifteen(monkeypatch):
    now = [200.0]
    server, mode, votes = _diamond_mode(monkeypatch, now)
    player = _Player(8, TEAM1, (120.5, 100.5, 50.5))
    player.connection = player
    server.players[player.id] = player
    asyncio.run(mode.on_mode_start())
    assert int(CG.DIA_DIAMONDS_TO_TRIGGER_MAP_VOTE) == 12
    assert mode.map_vote_trigger_score() == 12

    server.teams[TEAM1].score = 10
    _cash_one(mode, player, now, 1)  # 11: too early
    assert votes == []
    _cash_one(mode, player, now, 3)  # 12: the retail trigger
    assert len(votes) == 1
    # Each depleted drop-off rotation announced the new base.
    assert _ids(server.packets).count("DIAMOND_BASE") == 2


def test_diamond_map_vote_keeps_the_retail_lead_for_other_targets():
    mode = DiamondMineMode.__new__(DiamondMineMode)
    mode.score_limit = 5
    assert mode.map_vote_trigger_score() == 2
    mode.score_limit = 2
    assert mode.map_vote_trigger_score() == 1


def test_demolition_build_phase_drives_the_hud_clock(monkeypatch):
    from modes.demolition import DemolitionMode
    from tests.test_demolition import _Server as DemServer

    now = [100.0]
    monkeypatch.setattr("modes.demolition.time.time", lambda: now[0])
    server = DemServer()
    mode = DemolitionMode(server)
    asyncio.run(mode.on_mode_start())
    assert mode.phase == "building"
    timers = [
        DisplayCountdown(ByteReader(data[1:])).timer
        for data in server.packets
        if data and data[0] == DisplayCountdown.id
    ]
    assert timers and abs(timers[-1] - mode.build_state_length) < 1e-3
    now[0] = 110.0
    assert abs(
        mode.countdown_seconds_remaining(now[0]) - (mode.build_state_length - 10.0)
    ) < 1e-3


def test_zombie_headcount_shows_player_counts_and_start_cues_by_team():
    from server.builders.state_data import build_state_data

    server, mode = zombie_fixture._new_mode(player_count=3)
    state = build_state_data(server, player_id=1)
    assert state.team_headcount_type == int(C.TEAM_PLAYERS_COUNT_VALUE) == 0
    assert mode.start_cue_for(SimpleNamespace(team=SURVIVOR_TEAM)) == (
        "ZOMBIE_START_SURVIVOR"
    )
    assert mode.start_cue_for(SimpleNamespace(team=ZOMBIE_TEAM)) == (
        "ZOMBIE_START_ZOMBIE"
    )
    assert mode.start_cue_for(SimpleNamespace(team=0)) is None
    # The survivors heard their start line when the outbreak clock armed,
    # before the queued (non-overriding) virus line.
    ids = _ids(server.packets) + [
        string_id
        for player in server.players.values()
        for string_id in _ids(getattr(player.connection, "sent", []))
    ]
    assert "ZOMBIE_START_SURVIVOR" in ids


def _music(rows):
    names = []
    for data in rows:
        if data and data[0] == StopMusic.id:
            names.append("stop")
        elif data and data[0] == PlayMusic.id:
            names.append(PlayMusic(ByteReader(data[1:])).name)
    return names


def test_mode_start_music_switch():
    from server.audio import GAMEPLAY_TRACKS, gameplay_bed_track, play_gameplay_music

    packets = []
    server = SimpleNamespace(
        config=SimpleNamespace(mode_start_music=True),
        broadcast=lambda data, **_kw: packets.append(bytes(data)),
    )
    play_gameplay_music(server)
    assert _music(packets)[-1] in GAMEPLAY_TRACKS
    assert gameplay_bed_track(server) in GAMEPLAY_TRACKS

    packets.clear()
    server.config.mode_start_music = False
    play_gameplay_music(server)
    assert _music(packets) == ["stop"]  # retail: silence, old track ended
    assert gameplay_bed_track(server) is None
