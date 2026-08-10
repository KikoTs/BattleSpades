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
