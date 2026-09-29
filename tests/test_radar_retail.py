"""Retail Scout radar station: client-side detection and owner replacement.

Evidence (docs/RETAIL_VALUES.md "Radar station"): the stock client's Minimap
asks each of the viewer team's ``RadarStationEntity`` objects
``can_detect_player`` (250 blocks) for every enemy, so the server sends only
the packet-21 entity. Verified live 2026-09-26 with packet 83 suppressed:
enemies at 36-95 blocks detected, the same enemies at 289-403 blocks not.
The AoS wiki: a station self-destructs after 45 s, when shot enough, or when
the same player places another one.
"""

from __future__ import annotations

import asyncio
import inspect
import struct

import shared.constants as C
from protocol.packet_handler import PacketHandler
from server.config import ServerConfig
from server.connection import internal_team_to_wire
from server.entities.registry import send_create_entity_to
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from shared.bytes import ByteReader
from shared.packet import CreateEntity, DestroyEntity
from tests.test_equipment_handlers import _Connection, _server_player

TEAM_MAP_VISIBILITY = 83


def _place(server, player, x, y, z):
    packet = bytes([91]) + struct.pack("<IBHHH", 10, player.id, x, y, z)
    asyncio.run(PacketHandler(server).handle(player, packet))


def _radars(server):
    return [
        entity for entity in server.entity_registry.all()
        if entity.type == C.RADAR_STATION_ENTITY
    ]


def _radar_server(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(
        "server.deployable_actions.time.monotonic", lambda: now[0]
    )
    server, player, connection = _server_player(
        C.RADAR_STATION_TOOL, [C.RADAR_STATION_TOOL]
    )
    return server, player, connection, now


def _restock_and_wait(player, now):
    now[0] += float(C.RADAR_STATION_SHOOT_INTERVAL)
    player.restock_ammo(int(C.AMMO_CRATE))


def test_radar_entity_carries_what_the_client_detection_needs(monkeypatch):
    server, player, connection, _now = _radar_server(monkeypatch)

    _place(server, player, 101, 100, 62)

    (radar,) = _radars(server)
    creates = [
        CreateEntity(ByteReader(packet[1:])).entity
        for packet in connection.sent
        if packet[0] == CreateEntity.id
    ]
    wire = creates[-1]
    # can_detect_player compares the radar's team and position; set_fuse
    # drives the countdown and the client-side removal.
    assert wire.type == int(C.RADAR_STATION_ENTITY) == 36
    assert wire.state == internal_team_to_wire(TEAM1)
    assert (wire.pos_x, wire.pos_y, wire.pos_z) == (radar.x, radar.y, radar.z)
    assert wire.fuse == float(C.RADAR_STATION_LIFETIME) == 45.0
    # No range-less whole-team reveal to anyone.
    assert not any(packet[0] == TEAM_MAP_VISIBILITY for packet in connection.sent)


def test_server_has_no_radar_team_visibility_path():
    assert not hasattr(BattleSpadesServer, "_send_radar_visibility")
    source = inspect.getsource(BattleSpadesServer.reveal_world_to)
    assert "TeamMapVisibility" not in source
    assert "_radar_station_counts" not in source


def test_new_radar_replaces_the_owners_live_station(monkeypatch):
    server, player, connection, now = _radar_server(monkeypatch)
    _place(server, player, 101, 100, 62)
    (first,) = _radars(server)
    assert first.alive

    _restock_and_wait(player, now)
    connection.sent.clear()
    _place(server, player, 102, 100, 62)

    (second,) = _radars(server)
    assert second.entity_id != first.entity_id
    assert first.alive is False
    assert server.entity_registry.get(first.entity_id) is None
    assert player._radar_entity_id == second.entity_id
    assert server._radar_station_counts[TEAM1] == 1
    destroys = [
        DestroyEntity(ByteReader(packet[1:])).entity_id
        for packet in connection.sent
        if packet[0] == DestroyEntity.id
    ]
    creates = [
        CreateEntity(ByteReader(packet[1:])).entity.entity_id
        for packet in connection.sent
        if packet[0] == CreateEntity.id
    ]
    assert destroys == [first.entity_id]
    assert creates == [second.entity_id]
    # Old model removed before the new one appears.
    kinds = [packet[0] for packet in connection.sent
             if packet[0] in (DestroyEntity.id, CreateEntity.id)]
    assert kinds == [DestroyEntity.id, CreateEntity.id]
    assert not any(packet[0] == TEAM_MAP_VISIBILITY for packet in connection.sent)


def test_invalid_new_placement_keeps_the_old_station(monkeypatch):
    server, player, connection, now = _radar_server(monkeypatch)
    _place(server, player, 101, 100, 62)
    (first,) = _radars(server)

    _restock_and_wait(player, now)
    connection.sent.clear()
    # Beyond RADAR_STATION_FAR_RADIUS (10) from the owner.
    _place(server, player, 140, 100, 62)

    assert _radars(server) == [first]
    assert first.alive
    assert player._radar_entity_id == first.entity_id
    assert not any(packet[0] == DestroyEntity.id for packet in connection.sent)


def test_replacement_without_stock_is_refused(monkeypatch):
    server, player, _connection, now = _radar_server(monkeypatch)
    _place(server, player, 101, 100, 62)
    (first,) = _radars(server)

    now[0] += float(C.RADAR_STATION_SHOOT_INTERVAL)
    _place(server, player, 102, 100, 62)

    assert _radars(server) == [first]
    assert first.alive


def test_lifetime_comes_from_config_without_a_stale_fallback(monkeypatch):
    assert ServerConfig().radar_station_lifetime_seconds == float(
        C.RADAR_STATION_LIFETIME
    )
    server, player, _connection, _now = _radar_server(monkeypatch)
    server.config.radar_station_lifetime_seconds = 30.0

    _place(server, player, 101, 100, 62)

    (radar,) = _radars(server)
    assert radar.fuse == 30.0
    assert radar.behavior.lifetime == 30.0
    from server import deployable_actions

    assert "35.0" not in inspect.getsource(
        deployable_actions.DeployableActionService.place_radar
    )


def test_late_joiner_replay_counts_down_from_the_remaining_lifetime(monkeypatch):
    server, player, _connection, _now = _radar_server(monkeypatch)
    _place(server, player, 101, 100, 62)
    (radar,) = _radars(server)

    context = server._build_entity_ctx()
    context.now = 5000.0
    radar.behavior.on_tick(radar, 0.0, context)  # arms the expiry
    context.now = 5030.0
    radar.behavior.on_tick(radar, 0.0, context)
    assert radar.alive
    assert abs(radar.fuse - 15.0) < 1e-6

    joiner = _Connection(server)
    joiner.known_entity_ids = set()
    assert send_create_entity_to(joiner, radar) is True
    wire = CreateEntity(ByteReader(joiner.sent[-1][1:])).entity
    assert wire.type == int(C.RADAR_STATION_ENTITY)
    assert abs(wire.fuse - 15.0) < 1e-3
    assert wire.state == internal_team_to_wire(TEAM1)


def test_teardown_is_idempotent_for_the_team_count(monkeypatch):
    server, player, _connection, _now = _radar_server(monkeypatch)
    server._radar_station_counts[TEAM1] = 0
    _place(server, player, 101, 100, 62)
    (radar,) = _radars(server)
    assert server._radar_station_counts[TEAM1] == 1

    context = server._build_entity_ctx()
    radar.behavior.on_destroyed(radar, None, context)
    radar.behavior.on_destroyed(radar, None, context)
    radar.behavior.on_support_lost(radar, context)

    assert server._radar_station_counts[TEAM1] == 0
    assert player._radar_entity_id is None
