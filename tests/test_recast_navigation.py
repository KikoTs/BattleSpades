"""Native Recast/Detour smoke fixtures."""

import pytest


recast = pytest.importorskip("server.bot_ai.recast")


def test_flat_tile_build_and_path_query() -> None:
    navigator = recast.RecastNavigator()
    vertices = [
        0.0, 0.0, 0.0,
        32.0, 0.0, 0.0,
        32.0, 0.0, 32.0,
        0.0, 0.0, 32.0,
    ]
    triangles = [0, 2, 1, 0, 3, 2]

    assert navigator.ready is True
    assert navigator.build_tile(
        0, 0, vertices, triangles, (0.0, -2.0, 0.0), (32.0, 4.0, 32.0)
    )
    assert navigator.tile_count == 1

    path = navigator.find_path((2.0, 0.0, 2.0), (28.0, 0.0, 28.0))

    assert len(path) >= 6
    assert path[:3] == pytest.approx([2.0, 0.25, 2.0])
    assert path[-3:] == pytest.approx([28.0, 0.25, 28.0])


def test_tile_can_be_removed_after_terrain_collapse() -> None:
    navigator = recast.RecastNavigator()
    vertices = [0, 0, 0, 32, 0, 0, 32, 0, 32, 0, 0, 32]
    assert navigator.build_tile(
        0, 0, vertices, [0, 2, 1, 0, 3, 2], (0, -2, 0), (32, 4, 32)
    )

    assert navigator.remove_tile(0, 0)
    assert navigator.tile_count == 0
    assert navigator.find_path((2, 0, 2), (28, 0, 28)) == []


def test_detour_crowd_returns_bounded_local_steering() -> None:
    navigator = recast.RecastNavigator()
    vertices = [0, 0, 0, 32, 0, 0, 32, 0, 32, 0, 0, 32]
    assert navigator.build_tile(
        0, 0, vertices, [0, 2, 1, 0, 3, 2], (0, -2, 0), (32, 4, 32)
    )

    steering = navigator.crowd_steer(
        7,
        (2.0, 0.0, 2.0),
        (28.0, 0.0, 2.0),
        max_speed=4.0,
        max_acceleration=12.0,
        delta_time=0.2,
    )

    assert len(steering) == 3
    assert steering[0] > 0.0
    assert sum(value * value for value in steering) ** 0.5 <= 4.01
    navigator.remove_crowd_agent(7)
