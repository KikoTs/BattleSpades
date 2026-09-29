"""Territory Control retail announcements, score target and HUD gating."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
import shared.constants_gamemode as CG

from modes.territory_control import TerritoryControlMode, territory_name
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL
from server.map_metadata import MapMetadata, MapZone
from server.team import Team
from shared.bytes import ByteReader
from shared.packet import LocalisedMessage, TerritoryBaseState


class _World:
    map_name = "TCTest"
    map = SimpleNamespace(source_z_shift=0)

    def __init__(self):
        self.map_metadata = MapMetadata()

    def team_base_anchor(self, team):
        return (64.0, 256.0, 50.0) if team == TEAM1 else (448.0, 256.0, 50.0)

    def dry_ground_anchor(self, x, y, search=24):
        return float(x), float(y), 50.0


class _Server:
    def __init__(self, bases=3):
        self.config = SimpleNamespace(
            mode_settings={"tc": {"max_active_bases": bases, "capture_rate": 1.0}},
            configured_time_limit=lambda _mode, fallback: fallback,
        )
        self.world_manager = _World()
        for index in range(bases):
            self.world_manager.map_metadata.neutral_base_zones.append(MapZone(
                "base", TEAM_NEUTRAL, 100.0 + 100.0 * index, 100.0, 50.0,
                (-5.0, 5.0, -5.0, 5.0, -8.0, 8.0), "zone",
            ))
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
    def __init__(self, in_game=True):
        self.in_game = in_game
        self.sent = []

    def send(self, data, **_kwargs):
        self.sent.append(bytes(data))


def _player(server, player_id, team, position, *, in_game=True, bot=False):
    player = SimpleNamespace(
        id=player_id, name=f"P{player_id}", team=team, alive=True, spawned=True,
        position=tuple(position), score=0,
        connection=None if bot else _Connection(in_game),
    )
    server.players[player_id] = player
    return player


def _messages(rows):
    return [
        (packet.string_id, list(packet.parameters))
        for packet in (
            LocalisedMessage(ByteReader(data[1:]))
            for data in rows if data and data[0] == LocalisedMessage.id
        )
    ]


def _states(rows):
    return [
        TerritoryBaseState(ByteReader(data[1:])).action
        for data in rows if data and data[0] == TerritoryBaseState.id
    ]


def _mode(monkeypatch, now, bases=3):
    monkeypatch.setattr("modes.territory_control.time.time", lambda: now[0])
    monkeypatch.setattr("server.profile_stats.score_changed", lambda *_a: None)
    server = _Server(bases)
    mode = TerritoryControlMode(server)
    asyncio.run(mode.on_mode_start())
    return server, mode


def test_score_limit_is_the_territory_count(monkeypatch):
    server, mode = _mode(monkeypatch, [100.0], bases=5)
    assert len(mode.territories) == 5
    assert mode.score_limit == 5


def test_reveal_sends_tc_start():
    server = _Server()
    mode = TerritoryControlMode(server)
    asyncio.run(mode.on_mode_start())
    connection = _Connection()
    mode.reveal_to(connection)
    assert [row[0] for row in _messages(connection.sent)] == ["TC_START"]


def test_territory_letters():
    assert [territory_name(i) for i in range(3)] == ["A", "B", "C"]


def test_team_entering_a_territory_shouts_once_per_cooldown(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    middle = mode.territories[1]
    far = (0.0, 0.0, 0.0)
    runner = _player(server, 3, TEAM1, middle.zone.center)
    mate = _player(server, 1, TEAM1, far)
    loading = _player(server, 2, TEAM1, far, in_game=False)
    enemy = _player(server, 4, TEAM2, far)
    _player(server, 5, TEAM2, far, bot=True)
    asyncio.run(mode._capture_tick(0.5))
    assert _messages(runner.connection.sent) == [("TC_ENTER_BASE_PLAYER", ["B"])]
    assert _messages(mate.connection.sent) == [("TC_ENTER_BASE_TEAMMATES", ["B", "P3"])]
    assert _messages(enemy.connection.sent) == [("TC_ENTER_BASE_OPPOSITION", ["B", "P3"])]
    assert loading.connection.sent == []
    assert _states(runner.connection.sent) == [int(C.TC_BASE_ENTERING)]

    # Leave and re-enter inside the retail cooldown: HUD state only.
    runner.position = far
    now[0] += 1.0
    asyncio.run(mode._capture_tick(0.0))
    runner.position = middle.zone.center
    now[0] += 1.0
    asyncio.run(mode._capture_tick(0.0))
    assert len(_messages(mate.connection.sent)) == 1
    runner.position = far
    asyncio.run(mode._capture_tick(0.0))
    runner.position = middle.zone.center
    now[0] += float(CG.TC_NEW_TEAM_ENTERS_SHOUT_COOLDOWN)
    asyncio.run(mode._capture_tick(0.0))
    assert len(_messages(mate.connection.sent)) == 2


def test_entering_own_territory_is_silent(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    blue_base = mode.territories[0]
    assert blue_base.owner == TEAM1
    player = _player(server, 1, TEAM1, blue_base.zone.center)
    asyncio.run(mode._capture_tick(0.5))
    assert _messages(player.connection.sent) == []


def test_neutral_capture_announcement_counts(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    middle = mode.territories[1]
    runner = _player(server, 1, TEAM1, middle.zone.center)
    enemy = _player(server, 2, TEAM2, (0.0, 0.0, 0.0))
    # Stock TC_CAPTURE_RATE: one capturer adds 1 % per 0.5 s tick, so one
    # ownership step takes 50 s.
    asyncio.run(mode._capture_tick(49.0))
    assert middle.owner == TEAM_NEUTRAL
    asyncio.run(mode._capture_tick(1.0))
    assert middle.owner == TEAM1
    assert ("TC_NEUTRALCAPTURED_CAPTURINGTEAM", ["B", "1", "3"]) in _messages(
        runner.connection.sent
    )
    assert ("TC_NEUTRALCAPTURED_LOSINGTEAM", ["B", "1", "3"]) in _messages(
        enemy.connection.sent
    )


def test_enemy_capture_announcement_and_win(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    green_base = mode.territories[2]
    middle = mode.territories[1]
    middle.owner, middle.progress = TEAM1, 0.0
    middle.last_non_neutral_owner = TEAM1
    runner = _player(server, 1, TEAM1, green_base.zone.center)
    enemy = _player(server, 2, TEAM2, (0.0, 0.0, 0.0))
    ends = []

    async def end(winner=None):
        ends.append(winner)
        mode.ended = True

    mode.on_mode_end = end
    for _ in range(200):
        if mode.ended:
            break
        now[0] += 1.0
        asyncio.run(mode._capture_tick(1.0))
    assert green_base.owner == TEAM1
    captured = [row for row in _messages(runner.connection.sent)
                if row[0].startswith("TC_CAPTURED")]
    lost = [row for row in _messages(enemy.connection.sent)
            if row[0].startswith("TC_CAPTURED")]
    assert captured == [("TC_CAPTURED_CAPTURINGTEAM", ["C", "0", "3"])]
    assert lost == [("TC_CAPTURED_LOSINGTEAM", ["C", "0", "3"])]
    assert runner.score >= int(CG.TC_SCORE_CONTROL)
    assert ends == [TEAM1]


def test_kill_without_killer_is_ignored(monkeypatch):
    server, mode = _mode(monkeypatch, [100.0])
    victim = _player(server, 1, TEAM1, mode.territories[0].zone.center)
    asyncio.run(mode.on_player_kill(None, victim, 0))


def test_capture_amount_is_the_retail_attacker_percentage():
    """TerritoryBasesHud stretches the attacker plate to capture_amount/100."""
    from modes.territory_control import Territory, _wire_capture

    zone = SimpleNamespace(index=0)
    # Neutral base, Green halfway to claiming it.
    assert _wire_capture(Territory(zone=zone, owner=TEAM_NEUTRAL, progress=0.75)) == (
        TEAM2, 50.0)
    # Blue base, Green a quarter of the way to neutralising it.
    assert _wire_capture(Territory(zone=zone, owner=TEAM1, progress=0.125)) == (
        TEAM2, 25.0)
    # Green base under Blue attack, and an untouched base.
    assert _wire_capture(Territory(zone=zone, owner=TEAM2, progress=0.5)) == (
        TEAM1, 100.0)
    assert _wire_capture(Territory(zone=zone, owner=TEAM1, progress=0.0))[1] == 0.0


def test_capture_update_packets_carry_a_growing_percentage(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    middle = mode.territories[1]
    _player(server, 7, TEAM2, (200.0, 100.0, 50.0))
    server.packets.clear()
    asyncio.run(mode._capture_tick(1.0))
    rows = [
        TerritoryBaseState(ByteReader(data[1:]))
        for data in server.packets if data and data[0] == TerritoryBaseState.id
    ]
    updates = [row for row in rows if row.action == int(C.TC_BASE_CAPTURE_UPDATE)]
    assert updates and updates[-1].base_index == middle.zone.index
    assert updates[-1].attacked_by == TEAM2
    assert 1.0 < updates[-1].capture_amount < 100.0
