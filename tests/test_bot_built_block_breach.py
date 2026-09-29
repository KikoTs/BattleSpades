"""Bot dig plans budget the extra swings player-built (health 9) blocks cost."""

from __future__ import annotations

from types import SimpleNamespace

import shared.constants as C
from server.block_damage_model import footprint, wire_damage
from server.bot_ai.director import BotDirector
from server.bot_ai.messages import MovementAffordance, VoxelChange, WorldDelta
from server.dig_profiles import (
    BUILT_BLOCK_HEALTH,
    MAP_BLOCK_HEALTH,
    PRIMARY_DIG_PROFILES,
    melee_dig_positions,
)
from tests.test_simple_bot_navigation import (
    _FixtureVxl,
    _dig_profile,
    _movement_abilities,
    _sealed_wall_solids,
    _world,
)

WALL = tuple((13, y, z) for y in range(8, 13) for z in (17, 18, 19))


class _MutableFixtureVxl(_FixtureVxl):
    def set_solid(self, x, y, z, solid):
        if solid:
            self.solids.add((int(x), int(y), int(z)))
        else:
            self.solids.discard((int(x), int(y), int(z)))

    def surface_z(self, x, y):
        return min((z for cx, cy, z in self.solids if (cx, cy) == (x, y)),
                   default=239)


def _breach_for(tool_id: int, *, built: bool):
    world = _world(_sealed_wall_solids())
    world._vxl = _MutableFixtureVxl(world._vxl.solids)
    if built:
        # The wall arrives as canonical terrain deltas carrying the recorded
        # player-built health, exactly as the director publishes them.
        world.map_epoch = 0
        world.topology_version = 0
        world.apply(WorldDelta(0, 1, tuple(
            VoxelChange(*cell, True, 0x808080, health=BUILT_BLOCK_HEALTH)
            for cell in WALL
        )))
    observer = SimpleNamespace(can_shoot=True, loadout=(tool_id,))
    plan = world.plan(
        (12.5, 10.5, 17.75),
        (17.5, 10.5, 17.75),
        abilities=_movement_abilities(observer),
        dig_profile=_dig_profile(observer),
    )
    breaches = [step.breach for step in plan.steps
                if step.affordance is MovementAffordance.BREACH]
    assert breaches, f"tool {tool_id} found no breach"
    return world, breaches[0]


def test_built_wall_costs_more_swings_than_map_terrain() -> None:
    expectations = (
        # tool, map swings, built swings
        (int(C.SPADE_TOOL), 1, 2),
        (int(C.ZOMBIEHAND_TOOL), 1, 2),
        # Stock pickaxe block damage is 7 (the modded nonsteam value was 9):
        # one hit per 5-health map voxel, two per 9-health built voxel.
        (int(C.PICKAXE_TOOL), 2, 4),
        (int(C.KNIFE_TOOL), 10, 18),
    )
    for tool_id, map_swings, built_swings in expectations:
        _world_map, natural = _breach_for(tool_id, built=False)
        world, built = _breach_for(tool_id, built=True)
        assert natural.estimated_swings == map_swings, tool_id
        assert built.estimated_swings == built_swings, tool_id
        assert world.block_health(13, 10, 18) == BUILT_BLOCK_HEALTH
        assert world.block_health(14, 10, 18) == MAP_BLOCK_HEALTH


def test_removed_built_cell_forgets_its_health() -> None:
    world, _breach = _breach_for(int(C.SPADE_TOOL), built=True)
    world.apply(WorldDelta(0, 2, (VoxelChange(13, 10, 18, False),)))
    assert world.block_health(13, 10, 18) == MAP_BLOCK_HEALTH


def test_zombie_hand_breach_timeout_covers_every_retail_damage_roll() -> None:
    """Zombie hands (2 + 8r per cell) must not give up on a built wall.

    Replays the retail cube footprint for every Damage seed until the aimed
    built blocker breaks, and checks the executor's breach timeout
    (simple_worker: max(3, swings * interval * 3 + 1.5)) covers the slowest.
    """

    profile = PRIMARY_DIG_PROFILES[int(C.ZOMBIEHAND_TOOL)]
    _world_state, breach = _breach_for(int(C.ZOMBIEHAND_TOOL), built=True)
    covered = [cell for cell in breach.blocking_cells
               if cell in melee_dig_positions(breach.target_cell, profile.pattern)]
    amount = wire_damage(profile.block_damage)
    slowest = 0
    for first_seed in range(256):
        damage = {cell: 0.0 for cell in covered}
        swings = 0
        while any(total < BUILT_BLOCK_HEALTH for total in damage.values()):
            seed = (first_seed + swings) & 0xFF
            for cell, value in footprint(
                profile.damage_type, breach.target_cell, amount, seed
            ):
                if cell in damage:
                    damage[cell] += value
            swings += 1
        slowest = max(slowest, swings)
    assert slowest <= profile.swings_for_health(BUILT_BLOCK_HEALTH, worst_case=True)
    timeout = max(3.0, breach.estimated_swings * breach.fire_interval * 3.0 + 1.5)
    assert slowest * breach.fire_interval < timeout


def test_director_publishes_built_block_health_to_the_worker() -> None:
    published = []
    director = object.__new__(BotDirector)
    director._topology_version = 0
    director._map_epoch = 3
    director.supervisor = SimpleNamespace(
        publish_world_change=lambda change, **_: published.append(change)
    )
    director.server = SimpleNamespace(world_manager=SimpleNamespace(
        block_health={(5, 6, 7): BUILT_BLOCK_HEALTH}
    ))

    director._on_world_mutation(5, 6, 7, True, 0x123456, 1)
    director._on_world_mutation(8, 6, 7, True, 0x123456, 2)
    director._on_world_mutation(5, 6, 7, False, 0, 3)

    assert [change.health for change in published] == [BUILT_BLOCK_HEALTH, 0.0, 0.0]
