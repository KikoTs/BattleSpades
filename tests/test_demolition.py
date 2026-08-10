"""Demolition build, base-health, repair, and HUD regressions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants_gamemode as CG

from modes.demolition import DemolitionMode
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL
from server.map_metadata import MapMetadata, MapZone
from server.team import Team
from shared.bytes import ByteReader
from shared.packet import LockToZone, MinimapZone, TeamProgress


class _Connection:
    def __init__(self):
        self.player = None
        self.sent = []

    def send(self, data, **_kwargs):
        self.sent.append(bytes(data))


class _World:
    map_name = "AuthoredDemolition"
    map = SimpleNamespace(source_z_shift=0)

    def __init__(self):
        self.map_metadata = MapMetadata()
        self.map_metadata.base_zones[TEAM1].append(MapZone(
            "base", TEAM1, 10.0, 20.0, 30.0,
            (0, 1, 0, 1, 0, 1), "blue_base",
        ))
        self.map_metadata.base_zones[TEAM2].append(MapZone(
            "base", TEAM2, 40.0, 50.0, 30.0,
            (0, 1, 0, 1, 0, 1), "green_base",
        ))
        self.solids = {
            (10, 20, 30), (11, 20, 30),
            (40, 50, 30), (41, 50, 30),
        }
        self.listeners = {}
        self.next_token = 1

    def get_solid(self, x, y, z):
        return (int(x), int(y), int(z)) in self.solids

    def subscribe_mutations(self, callback):
        token = self.next_token
        self.next_token += 1
        self.listeners[token] = callback
        return token

    def unsubscribe_mutations(self, token):
        self.listeners.pop(token, None)

    def mutate(self, position, solid):
        if solid:
            self.solids.add(position)
        else:
            self.solids.discard(position)
        for callback in tuple(self.listeners.values()):
            callback(*position, bool(solid), 0, 1)


class _Server:
    def __init__(self):
        self.config = SimpleNamespace(
            mode_settings={"dem": {"build_state_length": 30.0}},
            configured_time_limit=lambda _mode, fallback: fallback,
        )
        self.world_manager = _World()
        self.teams = {
            TEAM1: Team(TEAM1, "Blue", (0, 0, 255)),
            TEAM2: Team(TEAM2, "Green", (0, 255, 0)),
        }
        self.players = {}
        self.packets = []

    def broadcast(self, data, **_kwargs):
        self.packets.append(bytes(data))

    def broadcast_set_score(self, team, reason=None):
        pass


def _decode(rows, packet_type):
    return [
        packet_type(ByteReader(data[1:]))
        for data in rows
        if data and data[0] == packet_type.id
    ]


def test_demolition_build_phase_locks_team_and_shares_both_base_indicators(
    monkeypatch,
):
    now = [100.0]
    monkeypatch.setattr("modes.demolition.time.time", lambda: now[0])
    server = _Server()
    connection = _Connection()
    player = SimpleNamespace(id=1, team=TEAM1, connection=connection)
    connection.player = player
    server.players[player.id] = player
    mode = DemolitionMode(server)
    asyncio.run(mode.on_mode_start())

    zones = _decode(server.packets, MinimapZone)
    assert len(zones) == 2
    assert {zone.key for zone in zones} == {TEAM_NEUTRAL}
    assert all(zone.icon_id == int(CG.ZONE_ICON_DEMOLITION) for zone in zones)
    assert all(zone.locked_in_zone == 1 for zone in zones)
    lock = _decode(connection.sent, LockToZone)[-1]
    assert (
        lock.A2018, lock.A2019, lock.A2020,
        lock.A2021, lock.A2022, lock.A2023,
    ) == mode.base_zones[TEAM1].bounds


def test_demolition_tracks_destroy_and_repair_with_native_team_progress(
    monkeypatch,
):
    now = [200.0]
    monkeypatch.setattr("modes.demolition.time.time", lambda: now[0])
    server = _Server()
    mode = DemolitionMode(server)
    asyncio.run(mode.on_mode_start())
    now[0] = 231.0
    asyncio.run(mode.on_tick(1))

    assert mode.phase == "active"
    assert len(mode.objective_cells[TEAM1]) == 2
    server.world_manager.mutate((10, 20, 30), False)
    asyncio.run(mode.on_tick(2))
    blue = next(
        packet for packet in _decode(server.packets, TeamProgress)
        if packet.team_id == TEAM1
    )
    assert (blue.numerator, blue.denominator, blue.icon_id) == (0, 2, 0)
    blue = [
        packet for packet in _decode(server.packets, TeamProgress)
        if packet.team_id == TEAM1
    ][-1]
    assert (blue.numerator, blue.denominator) == (1, 2)

    server.world_manager.mutate((10, 20, 30), True)
    asyncio.run(mode.on_tick(3))
    repaired = [
        packet for packet in _decode(server.packets, TeamProgress)
        if packet.team_id == TEAM1
    ][-1]
    assert repaired.numerator == 0


def test_demolition_full_destruction_enters_retail_airstrike_delay(monkeypatch):
    now = [300.0]
    monkeypatch.setattr("modes.demolition.time.time", lambda: now[0])
    server = _Server()
    mode = DemolitionMode(server)
    asyncio.run(mode.on_mode_start())
    now[0] = 331.0
    asyncio.run(mode.on_tick(1))
    server.world_manager.mutate((40, 50, 30), False)
    server.world_manager.mutate((41, 50, 30), False)
    asyncio.run(mode.on_tick(2))

    assert mode.phase == "airstrike"
    assert mode._destroyed_team == TEAM2
    assert mode._airstrike_at == now[0] + float(CG.DEM_TIME_TO_WAIT_FOR_AIRSTRIKE)
