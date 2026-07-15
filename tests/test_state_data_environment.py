"""Map-owned fog selection for the retail StateData snapshot."""

from __future__ import annotations

from types import SimpleNamespace

from server.builders.state_data import build_state_data
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.map_metadata import MapMetadata
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
