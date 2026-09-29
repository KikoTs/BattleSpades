"""Server block-damage model for prefab cells (retail DEFAULT_PREFAB_HEALTH).

The stock client adds every BuildPrefabAction(30) voxel to
``BlockManager.user_blocks`` with health 9.0 (live 2026-09-26) and removes a
cell when accumulated damage reaches that health.  WorldManager must break
the same cell after the same damage, or clients keep a block the server
already deleted (or vice versa).
"""

from __future__ import annotations

import pytest

import shared.constants as C
from server.config import ServerConfig
from server.game_constants import DEFAULT_BLOCK_HEALTH
from server.world_manager import WorldManager

PREFAB_HEALTH = float(C.DEFAULT_PREFAB_HEALTH)


@pytest.fixture()
def world():
    manager = WorldManager(ServerConfig())
    manager.generate_flat_map()
    return manager


def _cell(world, x=40, y=40):
    return x, y, int(world.get_height(x, y)) - 1


def _hits_to_break(world, cell, damage):
    hits = 0
    while world.get_solid(*cell):
        hits += 1
        world.apply_block_damage(*cell, damage, threshold=DEFAULT_BLOCK_HEALTH)
        assert hits < 100
    return hits


def test_prefab_cell_breaks_at_prefab_health(world):
    prefab = _cell(world, 40, 40)
    ordinary = _cell(world, 42, 40)
    assert world.set_block(*prefab, True, 0x808080, health=PREFAB_HEALTH)
    assert world.set_block(*ordinary, True, 0x808080)

    assert world.initial_block_health(*prefab) == PREFAB_HEALTH
    assert world.initial_block_health(*ordinary) == DEFAULT_BLOCK_HEALTH
    # Knife: 1 damage per hit -> 9 hits on a prefab cell, 5 on ordinary.
    assert _hits_to_break(world, prefab, 1.0) == 9
    assert _hits_to_break(world, ordinary, 1.0) == 5
    assert prefab not in world.block_health


def test_partial_damage_is_reported_as_remaining_health(world):
    cell = _cell(world)
    world.set_block(*cell, True, 0x808080, health=PREFAB_HEALTH)
    total, destroyed = world.apply_block_damage(*cell, 7.0)
    assert (total, destroyed) == (7.0, False)
    assert list(world.iter_block_health_state()) == [
        (*cell, PREFAB_HEALTH - 7.0)
    ]
    assert world.apply_block_damage(*cell, 2.0) == (9.0, True)
    assert list(world.iter_block_health_state()) == []


def test_recolour_keeps_health_but_new_topology_clears_it(world):
    cell = _cell(world)
    world.set_block(*cell, True, 0x808080, health=PREFAB_HEALTH)
    # Paint path: solid -> solid without a health argument.
    world.set_block(*cell, True, 0x102030)
    assert world.initial_block_health(*cell) == PREFAB_HEALTH

    world.destroy_blocks([cell])
    assert cell not in world.block_health
    # An ordinary block rebuilt in the same place is ordinary again.
    world.set_block(*cell, True, 0x808080)
    assert world.initial_block_health(*cell) == DEFAULT_BLOCK_HEALTH

    world.set_block(*cell, True, 0x808080, health=PREFAB_HEALTH)
    world.set_block(*cell, False)
    assert cell not in world.block_health


def test_prefab_over_existing_solid_resets_damage_and_health(world):
    cell = _cell(world)
    world.set_block(*cell, True, 0x808080)
    world.apply_block_damage(*cell, 4.0)
    # replace_solids: the prefab voxel overwrites the damaged block.
    world.set_block(*cell, True, 0x808080, health=PREFAB_HEALTH)
    assert world.block_damage.get(cell) is None
    assert world.apply_block_damage(*cell, 8.0) == (8.0, False)


def test_rule_multiplier_scales_prefab_health(world, monkeypatch):
    from server import game_rules

    class _Rules:
        def get(self, name):
            assert name == "RULE_BLOCK_HEALTH"
            return 2.0

    monkeypatch.setattr(game_rules, "get_rules", lambda _config: _Rules())
    cell = _cell(world)
    world.set_block(*cell, True, 0x808080, health=PREFAB_HEALTH)
    assert world.apply_block_damage(*cell, 17.0) == (17.0, False)
    # Remaining health stays in the client's unscaled user-block units.
    assert list(world.iter_block_health_state()) == [(*cell, 0.5)]
    assert world.apply_block_damage(*cell, 1.0)[1] is True


def test_new_map_forgets_prefab_health(world):
    cell = _cell(world)
    world.set_block(*cell, True, 0x808080, health=PREFAB_HEALTH)
    world.apply_block_damage(*cell, 1.0)
    world.generate_flat_map()
    assert world.block_health == {} and world.block_damage == {}
