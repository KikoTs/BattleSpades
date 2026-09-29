"""Retail ``BlockManager.add_user_block`` mode rules (audit3 verify V8).

Stock gameScene ``add_user_block`` (IDA 0x10070530):

* UGC Map Creator: ``user_blocks.pop(key)`` -- the cell is untracked and
  breaks at the map default (DEFAULT_BLOCK_HEALTH x multiplier);
* Classic: ``health = DEFAULT_BLOCK_HEALTH`` (5) whatever the caller asked
  (build 9, prefab 9, BlockBuildColored 3);
* otherwise the caller's health.
"""

from __future__ import annotations

import pytest

from server.combat_runtime import USER_BLOCK_HEALTH
from server.config import ServerConfig
from server.game_constants import DEFAULT_BLOCK_HEALTH
from server.main import BLOCK_COLORED_HEALTH, BattleSpadesServer
from server.runtime_vxl import ServerVXL

CELL = (100, 100, 100)


def _server(mode="tdm", *, ugc=False):
    config = ServerConfig()
    config.default_mode = mode
    config.ugc_runtime = ugc
    instance = BattleSpadesServer(config)
    world = instance.world_manager
    world.map = ServerVXL(-1, b"", 0, 2)
    world.find_unsupported_chunks = lambda _removed: []
    return instance


@pytest.mark.parametrize("health", [USER_BLOCK_HEALTH, BLOCK_COLORED_HEALTH])
def test_standard_modes_keep_the_callers_user_block_health(health):
    world = _server("tdm").world_manager
    assert world.set_block(*CELL, True, 0x406080, health=health)
    assert world.initial_block_health(*CELL) == health
    user, _damaged = world.block_manager_rows()
    assert user == [(*CELL, health)]


@pytest.mark.parametrize("health", [USER_BLOCK_HEALTH, BLOCK_COLORED_HEALTH, 9.0])
def test_classic_user_blocks_have_the_map_default_health(health):
    world = _server("cctf").world_manager
    assert world.set_block(*CELL, True, 0x406080, health=health)
    assert world.initial_block_health(*CELL) == DEFAULT_BLOCK_HEALTH == 5
    user, _damaged = world.block_manager_rows()
    assert user == [(*CELL, float(DEFAULT_BLOCK_HEALTH))]
    # One spade hit (5) breaks a classic built block, as on the client.
    assert world.apply_block_damage(*CELL, 5.0) is not False
    assert not world.get_solid(*CELL)


def test_ugc_user_blocks_are_untracked():
    world = _server("tdm", ugc=True).world_manager
    assert world.set_block(*CELL, True, 0x406080, health=USER_BLOCK_HEALTH)
    assert CELL not in world.block_health
    assert world.initial_block_health(*CELL) == DEFAULT_BLOCK_HEALTH
    user, _damaged = world.block_manager_rows()
    assert user == []


def test_ugc_observers_get_no_user_rows():
    server = _server("tdm", ugc=True)
    sent = []
    server.broadcast = lambda data, **kwargs: sent.append(data)
    from server.combat_runtime import get_combat_system

    combat = get_combat_system(server)
    server.world_manager.set_block(*CELL, True, 0x406080, health=USER_BLOCK_HEALTH)
    combat._send_observer_block_health(object(), (CELL,))
    assert sent == []
