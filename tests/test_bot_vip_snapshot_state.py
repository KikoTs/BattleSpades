"""Only a currently active and spawned VIP can own a published mode marker."""

from types import SimpleNamespace

import pytest

from server.bot_ai.director import BotDirector


@pytest.mark.parametrize("alive,spawned,mode_alive,expected", (
    (True, True, True, True), (False, True, True, False),
    (True, False, True, False), (True, True, False, False),
))
def test_vip_objective_snapshot_retires_stale_round_or_unspawned_players(
        alive, spawned, mode_alive, expected):
    vip = SimpleNamespace(id=4, alive=alive, spawned=spawned, position=(20., 30., 40.))
    director = object.__new__(BotDirector)
    director.server = SimpleNamespace(
        mode=SimpleNamespace(vips={2: vip}, vip_alive={2: mode_alive}),
        world_manager=SimpleNamespace(), players={4: vip},
    )
    markers = [item for item in director._snapshot_objectives() if item.kind == "vip"]
    assert bool(markers) is expected
    if expected:
        assert markers[0].carrier_id == 4 and markers[0].position == vip.position
