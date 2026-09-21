"""Carrier capability changes must invalidate work from the pickup approach."""

from dataclasses import replace

import pytest
import shared.constants as C

from server.bot_ai.messages import BotActionKind, MovementAffordance, ObjectiveSnapshot
from server.bot_ai.simple_navigation import BreachPlan, RoutePlan, RouteStep, SimpleVoxelWorld
from server.bot_ai.simple_worker import (
    SimpleBotBrain, _BotState, _Goal, _dig_profile, _melee_tool, _movement_abilities,
)
from server.bot_ai.surface_corridor import SurfaceCorridorSearch
from tests.test_simple_bot_tactics import _frame, _player


HOME = (10.5, 20.5, 97.75)
FLAG = (50.5, 20.5, 97.75)
POSITION = (30.5, 20.5, 97.75)


class _Ground:
    def get_solid(self, _x, _y, z):
        return z >= 100


class _Cells:
    def __init__(self, cells):
        self.cells = cells

    def get_solid(self, x, y, z):
        return (x, y, z) in self.cells


def _world(terrain=None):
    world = SimpleVoxelWorld()
    world._vxl = terrain or _Ground()
    world.map_epoch = world.topology_version = 1
    return world


def _actor(*, carried=-1, can_shoot=True, position=POSITION):
    return replace(
        _player(1, 2, position, is_bot=True, weapon_tool=int(C.RIFLE_TOOL),
                loadout=(int(C.RIFLE_TOOL), int(C.SPADE_TOOL))),
        carried_entity_id=carried, can_shoot=can_shoot,
    )


def _ctf_frame(actor, *others, now=100., home=HOME, flag=FLAG):
    return replace(
        _frame(actor, *others, created_at=now), mode_id="ctf",
        behavior_version="cooperative",
        objectives=(ObjectiveSnapshot("ctf_base", 2, home),
                    ObjectiveSnapshot("ctf_intel", 2, home),
                    ObjectiveSnapshot("ctf_intel", 3, flag,
                                      carrier_id=actor.player_id if actor.carried_entity_id >= 0 else -1)),
    )


@pytest.mark.parametrize("before_carried,after_carried,before_shoot,after_shoot", [
    (-1, 99, True, False),
    (99, -1, False, True),
    (-1, 99, True, True),
    (99, 99, False, True),
    (99, 99, True, False),
], ids=["pickup", "drop", "pickup-with-shooting", "shooting-enabled", "shooting-disabled"])
def test_carrier_edge_replans_immediately_without_old_breach_corridor_or_escape(
    before_carried, after_carried, before_shoot, after_shoot,
):
    brain = SimpleBotBrain(_world())
    before = _actor(carried=before_carried, can_shoot=before_shoot)
    assert brain.decide(_ctf_frame(before)) is not None
    previous = brain._states[(1, 1)]
    old_goal = FLAG if after_carried >= 0 else HOME
    stale_breach = BreachPlan((30, 20, 100), (31, 20, 100), (31, 20, 98),
                              ((31, 20, 98),), int(C.SPADE_TOOL), False, .3, 1)
    previous.route = (RouteStep(old_goal, MovementAffordance.BREACH, stale_breach),)
    previous.route_index = 0
    previous.route_topology_version = 1
    previous.breach_key = ("pickup-side-wall",)
    previous.breach_started_at = 99.
    previous.next_breach_at = 105.
    previous.escape_goal = old_goal
    previous.escape_until = 110.
    previous.escape_search = (("pickup-side",), (), 0)
    previous.corridor = (old_goal,)
    previous.corridor_search = SurfaceCorridorSearch(bytes([100] * 4), 2, 2, 0, 3)
    previous.corridor_failed_goal = old_goal
    previous.planning_results[("pickup-side",)] = RoutePlan(previous.route, True, 1)

    after = replace(before, carried_entity_id=after_carried, can_shoot=after_shoot)
    # This edge arrives before the ordinary 8 Hz decision throttle expires.
    intent = brain.decide(_ctf_frame(after, now=100.01))
    assert intent is not None
    assert intent.debug_goal == (HOME if after_carried >= 0 else FLAG)
    assert intent.movement.direction[0] * (-1 if after_carried >= 0 else 1) > .9
    assert intent.action.kind is BotActionKind.NONE
    assert intent.movement.affordance is MovementAffordance.WALK
    current = brain._states[(1, 1)]
    assert all(step.breach is None for step in current.route)
    assert current.breach_key is None
    assert current.escape_goal is None and current.escape_search is None
    assert current.corridor == () and current.corridor_search is None
    assert current.corridor_failed_goal is None
    assert ("pickup-side",) not in current.planning_results


@pytest.mark.parametrize("can_shoot", [False, True])
def test_visible_close_enemy_does_not_steal_unarmed_carrier_return_route(can_shoot):
    actor = _actor(carried=99, can_shoot=can_shoot)
    enemy = _player(2, 3, (34.5, 20.5, 97.75))
    intent = SimpleBotBrain(_world()).decide(_ctf_frame(actor, enemy))
    assert intent is not None
    assert intent.debug_goal == HOME
    assert intent.movement.direction[0] < -.9
    assert intent.action.kind is (BotActionKind.FIRE if can_shoot else BotActionKind.NONE)
    if can_shoot:
        assert intent.look.visible and intent.look.target_player_id == enemy.player_id
    else:
        assert intent.look is None or not intent.look.visible
        assert intent.movement.affordance is not MovementAffordance.BREACH


def test_unarmed_carrier_cannot_plan_excavation_but_shooting_carrier_can():
    # An enclosed dry lane has no jump or walk route over its six-block wall.
    cells = {(x, y, 20) for x in range(5, 19) for y in range(5, 16)}
    cells.update((11, y, z) for y in range(5, 16) for z in range(14, 20))
    world = _world(_Cells(cells))
    start, finish = (9.5, 10.5, 17.75), (15.5, 10.5, 17.75)
    allowed = _actor(carried=99, position=start)
    restricted = replace(allowed, can_shoot=False)
    permitted_plan = world.plan(
        start, finish, abilities=_movement_abilities(allowed),
        dig_profile=_dig_profile(allowed), _prefer_existing_path=False,
    )
    assert permitted_plan.reached_segment_goal
    assert any(step.breach is not None for step in permitted_plan.steps)
    assert _melee_tool(restricted) is None
    assert _dig_profile(restricted) is None
    assert MovementAffordance.BREACH not in _movement_abilities(restricted)
    restricted_plan = world.plan(
        start, finish, abilities=_movement_abilities(restricted),
        dig_profile=_dig_profile(restricted), _prefer_existing_path=False,
    )
    assert not restricted_plan.reached_segment_goal
    assert all(step.breach is None for step in restricted_plan.steps)
    brain = SimpleBotBrain(world)
    for index in range(12):
        intent = brain.decide(_ctf_frame(restricted, now=100. + index * .25,
                                         home=finish, flag=start))
        assert intent is not None
        assert intent.action.kind not in {BotActionKind.FIRE, BotActionKind.MELEE,
                                          BotActionKind.ORIENTED}
        assert intent.movement.affordance is not MovementAffordance.BREACH
        assert all(step.breach is None for step in brain._states[(1, 1)].route)


def test_stale_breach_executor_rechecks_carrier_permission_before_swinging():
    cell = (31, 20, 98)
    world = _world(_Cells({cell, (30, 20, 100), (31, 20, 100)}))
    actor = _actor(carried=99, can_shoot=False)
    breach = BreachPlan((30, 20, 100), (31, 20, 100), cell, (cell,),
                        int(C.SPADE_TOOL), False, .3, 1)
    step = RouteStep((31.5, 20.5, 97.75), MovementAffordance.BREACH, breach)
    state = _BotState(1, 1, actor.life_id, route=(step,), breach_key=(cell,))
    goal = _Goal(("ctf_capture",), HOME, "ctf_capture", 4., True)
    assert world.solid(*cell)  # Rejection must come from capability, not removed terrain.
    intent = SimpleBotBrain(world)._breach_intent(
        _ctf_frame(actor), actor, state, goal, step, 100.,
    )
    assert intent.action.kind is BotActionKind.NONE
    assert intent.movement.direction == (0., 0., 0.)
    assert intent.debug_role == "ctf_capture:breach_replan"
    assert state.route == () and state.breach_key is None
