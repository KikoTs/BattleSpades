"""Multi-Hill objective, HUD, rotation, and late-join regressions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants_gamemode as CG

from modes.multi_hill import MultiHillMode
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL
from server.map_metadata import MapMetadata
from server.team import Team
from shared.bytes import ByteReader
from shared.packet import MinimapZone, MinimapZoneClear


class _World:
    map_name = "FlatTest"
    map = SimpleNamespace(source_z_shift=0)

    def __init__(self):
        self.map_metadata = MapMetadata()

    def team_base_anchor(self, team):
        return (64.0, 256.0, 50.0) if team == TEAM1 else (448.0, 256.0, 50.0)

    def dry_ground_anchor(self, x, y, search=24):
        return (float(x), float(y), 50.0)


class _Server:
    def __init__(self):
        self.config = SimpleNamespace(
            mode_settings={"mh": {"score_limit": 100}},
            configured_time_limit=lambda _mode, fallback: fallback,
        )
        self.world_manager = _World()
        self.teams = {
            TEAM1: Team(TEAM1, "Blue", (0, 0, 255)),
            TEAM2: Team(TEAM2, "Green", (0, 255, 0)),
        }
        self.players = {}
        self.packets = []
        self.score_updates = []

    def broadcast(self, data, **_kwargs):
        self.packets.append(bytes(data))

    def broadcast_set_score(self, team, reason=None):
        self.score_updates.append((team.id, team.score, reason))


class _Connection:
    def __init__(self):
        self.sent = []

    def send(self, data, **_kwargs):
        self.sent.append(bytes(data))


def _decode(rows, packet_type):
    return [
        packet_type(ByteReader(data[1:]))
        for data in rows
        if data and data[0] == packet_type.id
    ]


def test_multihill_uses_shared_native_indicator_and_scores_control(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("modes.multi_hill.time.time", lambda: now[0])
    server = _Server()
    mode = MultiHillMode(server)
    asyncio.run(mode.on_mode_start())

    assert len(mode.zones) >= 2
    initial = _decode(server.packets, MinimapZone)[-1]
    assert initial.key == TEAM_NEUTRAL
    assert initial.icon_id == int(CG.ZONE_ICON_MULTIHILL)
    assert initial.color == (255, 255, 255)

    zone = mode.active_zones[0]
    player = SimpleNamespace(
        id=7,
        team=TEAM1,
        alive=True,
        spawned=True,
        position=zone.center,
        score=0,
    )
    server.players[player.id] = player
    now[0] = 101.1
    asyncio.run(mode.on_tick(1))

    assert mode.zone_owner[zone.index] == TEAM1
    assert server.teams[TEAM1].score == 1
    owned = _decode(server.packets, MinimapZone)[-1]
    assert owned.key == TEAM_NEUTRAL
    assert owned.color == server.teams[TEAM1].color
    assert server.score_updates[-1][0:2] == (TEAM1, 1)


def test_multihill_rotation_clears_old_zone_and_late_join_gets_only_live_zone(
    monkeypatch,
):
    now = [200.0]
    monkeypatch.setattr("modes.multi_hill.time.time", lambda: now[0])
    server = _Server()
    mode = MultiHillMode(server)
    asyncio.run(mode.on_mode_start())
    old = mode.active_zones[0]

    now[0] = 200.0 + mode.base_active_time + 0.1
    asyncio.run(mode.on_tick(1))
    clears = _decode(server.packets, MinimapZoneClear)
    assert clears
    assert (clears[-1].A2018, clears[-1].A2019) == old.bounds[:2]
    assert mode.phase == "intermission"

    now[0] += float(CG.MH_TIME_BETWEEN_BASE_ACTIVATIONS) + 0.1
    asyncio.run(mode.on_tick(2))
    connection = _Connection()
    mode.reveal_to(connection)
    live = _decode(connection.sent, MinimapZone)
    assert len(live) == len(mode.active_zones)
    assert all(packet.key == TEAM_NEUTRAL for packet in live)


# ---------------------------------------------------------------------------
# 2026-09-25 fixes: restart clears, gated announcements, personal scoring,
# deterministic winner.
# ---------------------------------------------------------------------------


from shared.packet import LocalisedMessage


class _GameConnection(_Connection):
    def __init__(self, in_game=True):
        super().__init__()
        self.in_game = in_game


def _mh_player(server, player_id, team, position, *, in_game=True):
    player = SimpleNamespace(
        id=player_id, name=f"P{player_id}", team=team, alive=True, spawned=True,
        position=tuple(position), score=0,
        connection=_GameConnection(in_game),
    )
    server.players[player_id] = player
    return player


def _ids(rows):
    return [packet.string_id for packet in _decode(rows, LocalisedMessage)]


def _started(monkeypatch, now):
    monkeypatch.setattr("modes.multi_hill.time.time", lambda: now[0])
    monkeypatch.setattr("server.profile_stats.score_changed", lambda *_a: None)
    server = _Server()
    mode = MultiHillMode(server)
    asyncio.run(mode.on_mode_start())
    return server, mode


def test_restart_clears_previous_hill_icons(monkeypatch):
    now = [100.0]
    server, mode = _started(monkeypatch, now)
    old = mode.active_zones[0]
    server.packets.clear()
    asyncio.run(mode.on_mode_start())
    clears = _decode(server.packets, MinimapZoneClear)
    assert [(c.A2018, c.A2019, c.A2020, c.A2021) for c in clears] == [old.bounds[:4]]
    assert mode.phase == "active" and len(mode.active_zones) == 1


def test_reveal_sends_multi_hill_start():
    server = _Server()
    mode = MultiHillMode(server)
    asyncio.run(mode.on_mode_start())
    connection = _Connection()
    mode.reveal_to(connection)
    assert _ids(connection.sent) == ["MULTI_HILL_START"]


def test_claim_announcements_are_team_relative_and_gated(monkeypatch):
    now = [100.0]
    server, mode = _started(monkeypatch, now)
    zone = mode.active_zones[0]
    far = (0.0, 0.0, 0.0)
    blue = _mh_player(server, 1, TEAM1, zone.center)
    mate = _mh_player(server, 2, TEAM1, far)
    loading = _mh_player(server, 3, TEAM1, far, in_game=False)
    green = _mh_player(server, 4, TEAM2, far)
    mode._update_control(now[0])
    assert _ids(blue.connection.sent) == ["MULTIHILL_OCCUPIED_YOU"]
    assert _ids(mate.connection.sent) == ["MULTIHILL_OCCUPIED_FRIENDLY"]
    assert _ids(loading.connection.sent) == []
    assert _ids(green.connection.sent) == ["MULTIHILL_OCCUPIED_ENEMY"]
    # First to Hill (250) plus Claim Hill (150) for the neutral claim.
    assert blue.score == int(CG.MH_SCORE_FIRST) + int(CG.MH_SCORE_CLAIM)

    # Green takes it: the previous owners hear "Hill lost!", the decisive
    # green player earns the Control award (TC_SCORE_CONTROL analogue).
    blue.position = far
    green.position = zone.center
    mode._update_control(now[0])
    assert _ids(mate.connection.sent)[-1] == "MULTIHILL_LOST"
    assert _ids(green.connection.sent)[-1] == "MULTIHILL_OCCUPIED_YOU"
    assert green.score == int(CG.MH_SCORE_CONTROL)


def test_contested_shout_is_rate_limited(monkeypatch):
    now = [100.0]
    server, mode = _started(monkeypatch, now)
    zone = mode.active_zones[0]
    _mh_player(server, 1, TEAM1, zone.center)
    green = _mh_player(server, 2, TEAM2, zone.center)
    for step in range(4):
        green.position = zone.center if step % 2 == 0 else (0.0, 0.0, 0.0)
        now[0] += 0.5
        mode._update_control(now[0])
    assert _ids(server.packets).count("MULTIHILL_CONTESTED") == 1


def test_presence_awards_occupy_and_contest(monkeypatch):
    now = [100.0]
    server, mode = _started(monkeypatch, now)
    zone = mode.active_zones[0]
    blue = _mh_player(server, 1, TEAM1, zone.center)
    now[0] = 100.5
    asyncio.run(mode.on_tick(1))
    first = blue.score
    now[0] = 100.0 + float(CG.MH_SCORE_OCCUPY_INTERVAL) + 0.1
    asyncio.run(mode.on_tick(2))
    assert blue.score - first == int(CG.MH_SCORE_OCCUPY)

    green = _mh_player(server, 2, TEAM2, zone.center)
    now[0] += float(CG.MH_SCORE_OCCUPY_INTERVAL)
    asyncio.run(mode.on_tick(3))
    assert green.score == int(CG.MH_SCORE_CONTEST)
    assert blue.score - first == int(CG.MH_SCORE_OCCUPY) + int(CG.MH_SCORE_CONTEST)


def test_kills_on_the_hill_award_defend_and_assault(monkeypatch):
    now = [100.0]
    server, mode = _started(monkeypatch, now)
    # Isolate the hill extras from BaseMode's generic kill score.
    mode.award_generic_kill_score = lambda *_args: 0
    zone = mode.active_zones[0]
    blue = _mh_player(server, 1, TEAM1, zone.center)
    mode._update_control(now[0])
    green = _mh_player(server, 2, TEAM2, zone.center)
    base = blue.score
    asyncio.run(mode.on_player_kill(blue, green, 0))
    assert blue.score - base == int(CG.MH_SCORE_DEFEND)
    blue.position = (0.0, 0.0, 0.0)
    asyncio.run(mode.on_player_kill(green, blue, 0))
    assert green.score == 0  # killer outside, victim outside: no bonus
    blue.position = zone.center
    green.position = (0.0, 0.0, 0.0)
    asyncio.run(mode.on_player_kill(green, blue, 0))
    assert green.score == int(CG.MH_SCORE_ASSAULT)


def test_simultaneous_limit_is_deterministic(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("modes.multi_hill.time.time", lambda: now[0])
    server = _Server()
    server.config.mode_settings["mh"]["max_active_bases"] = 2
    mode = MultiHillMode(server)
    asyncio.run(mode.on_mode_start())
    first, second = mode.active_zones[:2]
    mode.zone_owner[first.index] = TEAM1
    mode.zone_owner[second.index] = TEAM2
    ends = []

    async def end(winner=None):
        ends.append(winner)
        mode.ended = True

    mode.on_mode_end = end
    server.teams[TEAM1].score = 99
    server.teams[TEAM2].score = 99
    asyncio.run(mode._award_team_ticks(101.0))
    assert ends == [None]
    assert "GAME_DRAWN" in _ids(server.packets)

    mode.ended = False
    ends.clear()
    mode._last_score_at = 101.0
    server.teams[TEAM1].score = 99
    server.teams[TEAM2].score = 100
    asyncio.run(mode._award_team_ticks(102.0))
    assert ends == [TEAM2]


def test_multihill_score_limit_comes_from_modes_overlay_not_a_phantom_rule():
    """Retail has no Multi-Hill score-target rule (the lobby's mh rules are
    only max active bases and base active time); [modes.mh] score_limit
    sets the target and mode_data's 100 is the default."""
    import shared.constants_matchmaking as MM
    from server.config import ServerConfig
    from server.game_rules import RULE_DEFINITIONS

    mh_rules = MM.GAME_RULES_NAMES["mh"]
    assert not any("SCORE" in rule for rule in mh_rules)
    assert all(rule in RULE_DEFINITIONS for rule in mh_rules)

    config = ServerConfig()
    server = _Server()
    server.config = config
    assert MultiHillMode(server).score_limit == 100

    config.mode_settings = {"mh": {"score_limit": 250}}
    assert MultiHillMode(server).score_limit == 250
