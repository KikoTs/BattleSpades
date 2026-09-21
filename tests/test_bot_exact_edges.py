"""Jumps and drops the planner authors are ones a body can really take."""

from server.bot_ai.messages import MovementAffordance
from server.bot_ai.simple_navigation import SimpleVoxelWorld


class _Vxl:
    def __init__(self, solids):
        self.solids = set(solids)

    def get_solid(self, x, y, z):
        return (int(x), int(y), int(z)) in self.solids


def _world(solids):
    world = SimpleVoxelWorld()
    world._vxl, world._atlas = _Vxl(solids), None
    return world


def _ground(z=100, columns=range(5, 25), rows=range(8, 14), gap=()):
    return {(x, y, depth) for x in columns for y in rows for depth in range(z, z + 3)
            if x not in gap}


def test_a_gap_is_not_jumped_through_a_head_height_opening_under_a_slab():
    open_gap = _world(_ground(gap={12}))
    assert open_gap._jump_gap_is_clear(11, 10, 1, 0, 2, 100, 100)
    # Standing room on both lips and an open gap, but a slab where the arc peaks.
    slab = _world(_ground(gap={12}) | {(12, y, 96) for y in range(8, 14)})
    assert not slab._jump_gap_is_clear(11, 10, 1, 0, 2, 100, 100)
    low_ceiling = _world(_ground(gap={12}) | {(13, y, 96) for y in range(8, 14)})
    assert not low_ceiling._jump_gap_is_clear(11, 10, 1, 0, 2, 100, 100)


def test_a_planned_drop_records_the_cardinal_edge_it_leaves_by():
    # A shelf three blocks above the ground to its east.
    solids = _ground(z=103) | {(x, y, z) for x in range(5, 12) for y in range(8, 14)
                               for z in range(100, 103)}
    plan = _world(solids).plan((8.5, 10.5, 97.75), (18.5, 10.5, 100.75),
                               abilities={MovementAffordance.WALK, MovementAffordance.DROP,
                                          MovementAffordance.JUMP},
                               dig_profile=None, allow_water=False, blocked_edges=frozenset())
    drops = [step for step in plan.steps if step.affordance is MovementAffordance.DROP]
    assert plan.reached_segment_goal and len(drops) == 1
    (source, target) = drops[0].entry_edge
    assert source[2] == 100 and target[2] == 103
    assert abs(target[0] - source[0]) + abs(target[1] - source[1]) == 1
