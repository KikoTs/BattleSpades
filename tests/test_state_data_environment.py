"""Map-owned fog selection for the retail StateData snapshot."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.bytes import ByteReader
from shared.packet import StateData
from server.builders.state_data import build_state_data
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.map_metadata import MapMetadata, load_map_metadata
from server.team import Team


def _server(map_fog, override=None):
    return SimpleNamespace(
        config=ServerConfig(default_mode="tdm", fog_color_rgb=(12, 13, 11)),
        mode=None,
        teams={
            TEAM1: Team(TEAM1, "Blue", (0, 0, 255)),
            TEAM2: Team(TEAM2, "Green", (0, 255, 0)),
        },
        world_manager=SimpleNamespace(
            map_metadata=MapMetadata(fog_color=map_fog),
        ),
        fog_color_override=override,
    )


def test_state_data_uses_active_map_fog():
    packet = build_state_data(_server((69, 76, 39)), player_id=7)

    assert packet.fog_color == (69, 76, 39)


def test_state_data_uses_the_authoritative_map_gravity():
    server = _server((69, 76, 39))
    server.world_manager.map_metadata.gravity = 26.0 / 64.0

    packet = build_state_data(server, player_id=7)

    assert packet.gravity == 26.0 / 64.0
    wire = bytes(packet.generate())
    decoded = StateData(ByteReader(wire[1:]))
    assert decoded.gravity == 26.0 / 64.0


def test_runtime_admin_fog_override_wins_over_map_metadata():
    packet = build_state_data(
        _server((69, 76, 39), override=(1, 2, 3)), player_id=7,
    )

    assert packet.fog_color == (1, 2, 3)


def test_state_data_uses_authored_map_lighting():
    server = _server((69, 76, 39))
    server.world_manager.map_metadata.light_color = (236, 244, 203)
    server.world_manager.map_metadata.light_direction = (-0.7, 0.3, 0.0)
    server.world_manager.map_metadata.back_light_color = (15, 20, 10)
    server.world_manager.map_metadata.back_light_direction = (0.0, 0.7, 0.3)
    server.world_manager.map_metadata.ambient_light_color = (15, 30, 10)
    server.world_manager.map_metadata.ambient_light_intensity = 0.3

    packet = build_state_data(server, player_id=7)

    assert packet.light_color == (236, 244, 203)
    assert packet.light_direction == (-0.7, 0.3, 0.0)
    assert packet.back_light_color == (15, 20, 10)
    assert packet.back_light_direction == (0.0, 0.7, 0.3)
    assert packet.ambient_light_color == (15, 30, 10)
    assert abs(packet.ambient_light_intensity - 0.3) < 1e-6


@pytest.mark.parametrize("name", ["ArcticBase", "London"])
def test_missing_map_lighting_preserves_pre_parity_wire_appearance(name):
    """A missing map palette must not borrow MayanJungle's warm lighting."""
    server = _server(None)
    metadata = load_map_metadata(Path("maps") / f"{name}.vxl", "tdm")
    assert metadata.light_color is None
    server.world_manager.map_metadata = metadata
    wire = bytes(build_state_data(server, player_id=7).generate())
    received = StateData(ByteReader(wire[1:]))

    # Compatibility baseline: the actual StateData values before 68c36ca.
    # This does not assert that an unrecovered retail map used these values.
    assert received.light_color == (180, 192, 220)
    assert received.light_direction == (13 / 64, 51 / 64, 0.0)
    assert received.back_light_color == (64, 64, 64)
    # Stock fixed encoding adds 0.5 before truncation even for negatives.
    assert received.back_light_direction == (-4 / 64, -36 / 64, 19 / 64)
    assert received.ambient_light_color == (52, 56, 64)
    assert received.ambient_light_intensity == 13 / 64


@pytest.mark.parametrize("name", ["MayanJungle", "Trenches"])
def test_recovered_map_lighting_survives_fallback_rollback(name):
    """Restoring the fallback must preserve maps with recovered lighting."""
    server = _server(None)
    server.world_manager.map_metadata = load_map_metadata(
        Path("maps") / f"{name}.vxl", "tdm"
    )
    wire = bytes(build_state_data(server, player_id=7).generate())
    received = StateData(ByteReader(wire[1:]))
    assert received.light_color == (236, 244, 203)
    assert received.light_direction == (-44 / 64, 19 / 64, 0.0)
    assert received.back_light_color == (15, 20, 10)
    assert received.back_light_direction == (0.0, 45 / 64, 19 / 64)
    assert received.ambient_light_color == (15, 30, 10)
    assert received.ambient_light_intensity == 19 / 64
