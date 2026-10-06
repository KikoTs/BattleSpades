"""No bot is stranded behind water; the infected also kill in it.

Retail drops SpookyMansion's zombies into the sea ring around the island and
counts their kills in water. The bots used to be carried from that spawn to
whatever islet the nearest-shore flow pointed at and never left it, and a
swimmer of any class came back to the bank it had left once the channel was
wider than one planned route.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest
import shared.constants as C

from scripts.bot_zombie_horde_scenario import run_scenario, scenario_args
from server.bot_ai.messages import BotActionKind, MovementAffordance
from server.bot_ai.navigation_atlas import NavigationAtlas
from server.bot_ai.simple_navigation import RouteStep
from server.bot_ai.simple_worker import (
    SimpleBotBrain,
    _BotState,
    _Goal,
    _TraversalStyle,
    _route_step_reached,
)
from server.game_constants import TEAM1, TEAM2
from tests.test_simple_bot_navigation import _world
from tests.test_simple_bot_tactics import _frame, _player

ZOMBIE = int(C.CLASS_ZOMBIE)
HAND = int(C.ZOMBIEHAND_TOOL)
BUILDER_KIT = (int(C.MINIGUN_TOOL), int(C.SPADE_TOOL), int(C.BLOCK_TOOL))
# Standing on the waterbed / the planner's water-plane waypoint height.
WADE_Z = 237.75
SWIM_Z = 236.75


def _zombie(player_id, position, **kwargs):
    return _player(player_id, TEAM1, position, class_id=ZOMBIE, is_bot=True,
                   loadout=(HAND, int(C.ZOMBIE_PREFAB_TOOL)), weapon_tool=HAND, **kwargs)


def _zombie_frame(observer, *players, created_at=100.0):
    return replace(_frame(observer, *players, created_at=created_at), mode_id="zom")


def _strait():
    """An islet (x 5-14), sixty blocks of open sea, a mainland (x 70-90).

    Both shores stand two blocks above the waterbed, like SpookyMansion's
    beaches: the shared shore flow calls that a bank for builders.
    """

    solids = {(x, y, 239) for x in range(5, 91) for y in range(5, 36)}
    for x in list(range(5, 15)) + list(range(70, 91)):
        solids.update((x, y, z) for y in range(5, 36) for z in (237, 238))
    return _world(solids)


def _with_atlas(world):
    """Give a fixture world the real map atlas (ground regions, shore flow)."""

    solids = world._vxl.solids
    columns: dict[tuple[int, int], int] = {}
    for x, y, z in solids:
        columns[(x, y)] = columns.get((x, y), 0) | (1 << z)
    world._atlas = NavigationAtlas.from_masks(
        (columns.get((x, y), 0) for y in range(512) for x in range(512)),
        width=512, height=512)
    return world


def _soldiers(position, *, wade=False):
    """Three team bots: ids 1, 2, 3 are the dry, swim and bridge identities."""

    return [_player(i, TEAM1, position, is_bot=True, wade=wade, grounded=not wade,
                    loadout=BUILDER_KIT)
            for i in (1, 2, 3)]


def _brain_with_goal(world, observer, goal_position):
    brain = SimpleBotBrain(world)
    brain.reset_for_map(1)
    state = _BotState(1, 1, observer.life_id)
    state.goal = _Goal(("objective", "far_shore"), goal_position, "objective", 3.0, True)
    brain._states[(observer.player_id, observer.generation)] = state
    return brain, state


def test_builders_with_nothing_to_build_with_swim_and_the_rest_keep_their_style():
    zombies = [_zombie(i, (10.0 + i, 10.0, 20.0)) for i in range(1, 7)]
    humans = [_player(10 + i, TEAM2, (40.0 + i, 10.0, 20.0), is_bot=True,
                      loadout=BUILDER_KIT) for i in range(6)]
    frame = _zombie_frame(zombies[0], *zombies[1:], *humans)
    brain = SimpleBotBrain(_strait())
    # The identity itself is untouched: a team still has all three.
    for team in (zombies, humans):
        assert {SimpleBotBrain._traversal_personality(frame, p).style for p in team} == {
            _TraversalStyle.DRY, _TraversalStyle.SWIM, _TraversalStyle.BRIDGE}
    # No Zombie carries a block tool: its builders swim, its dry runners
    # still take a bridge where there is one.
    styles = [brain._crossing_personality(frame, z).style for z in zombies]
    assert styles.count(_TraversalStyle.SWIM) == 4 and styles.count(_TraversalStyle.DRY) == 2
    assert {brain._crossing_personality(frame, h).style for h in humans} == {
        _TraversalStyle.DRY, _TraversalStyle.SWIM, _TraversalStyle.BRIDGE}
    # A builder out of blocks, or carrying no block tool, cannot bridge.
    for spent in ([replace(h, blocks=0) for h in humans],
                  [replace(h, loadout=BUILDER_KIT[:2]) for h in humans]):
        frame = _zombie_frame(zombies[0], *zombies[1:], *spent)
        assert {brain._crossing_personality(frame, h).style for h in spent} == {
            _TraversalStyle.DRY, _TraversalStyle.SWIM}


def test_a_swim_waypoint_is_reached_at_any_height_of_the_water_hop():
    step = RouteStep((30.5, 20.5, SWIM_Z), MovementAffordance.SWIM)
    # A Zombie's held ascent carries it five blocks above the waterbed.
    for height in (WADE_Z, 236.0, 234.0, 232.4):
        assert _route_step_reached(step, (30.7, 20.3, height), wading=True)
    assert not _route_step_reached(step, (32.0, 20.5, WADE_Z), wading=True)
    # Dry bodies keep the floor-level contract (a ledge above is not arrival).
    assert not _route_step_reached(step, (30.5, 20.5, 232.4), wading=False)


def test_a_wading_zombie_swims_the_whole_strait_toward_its_prey_and_hops_the_beach():
    world = _strait()
    brain = SimpleBotBrain(world)
    survivor = _player(9, TEAM2, (80.5, 20.5, 234.75))
    position = (20.5, 20.5, WADE_Z)
    now = 100.0
    westmost = position[0]
    bank_jump = False
    for _decision in range(160):
        observer = _zombie(1, position, wade=True, grounded=False)
        intent = brain.decide(_zombie_frame(observer, survivor, created_at=now))
        assert intent is not None
        direction = intent.movement.direction
        if position[0] >= 68.9:
            # Beside the two-block beach: a hop the Zombie's jump clears.
            bank_jump = bank_jump or (
                intent.movement.affordance is MovementAffordance.JUMP and direction[0] > 0.5)
            if bank_jump:
                break
        position = (position[0] + direction[0] * 0.8, position[1] + direction[1] * 0.8, WADE_Z)
        westmost = min(westmost, position[0])
        now += 0.13
    # Never turned back for the islet six cells behind it.
    assert westmost >= 19.5
    assert position[0] >= 68.9 and bank_jump, (position, intent.debug_role)


def test_a_wading_zombie_claws_a_survivor_standing_in_the_sea():
    brain = SimpleBotBrain(_strait())
    observer = _zombie(1, (40.5, 20.5, WADE_Z), wade=True, grounded=False)
    survivor = _player(9, TEAM2, (42.0, 20.5, WADE_Z), wade=True)
    intent = brain.decide(_zombie_frame(observer, survivor))
    assert intent is not None
    assert intent.action.kind is BotActionKind.MELEE and intent.action.tool_id == HAND
    assert intent.tool_id == HAND


def test_a_wading_zombie_closes_on_a_survivor_in_the_sea_instead_of_leaving_for_shore():
    brain = SimpleBotBrain(_strait())
    observer = _zombie(1, (40.5, 20.5, WADE_Z), wade=True, grounded=False)
    survivor = _player(9, TEAM2, (46.5, 20.5, WADE_Z), wade=True)
    intent = brain.decide(_zombie_frame(observer, survivor))
    assert intent is not None
    assert intent.movement.affordance is MovementAffordance.SWIM
    assert intent.movement.direction[0] > 0.9


def test_a_swimming_soldier_holds_its_bearing_across_a_channel_wider_than_one_route():
    world = _strait()
    far_shore = (80.5, 20.5, 234.75)
    dry, swimmer, builder = _soldiers((20.5, 20.5, WADE_Z), wade=True)
    brain, _state = _brain_with_goal(world, swimmer, far_shore)
    position, now, westmost = swimmer.position, 100.0, 20.5
    for _decision in range(140):
        observer = _player(2, TEAM1, position, is_bot=True, wade=True, grounded=False)
        intent = brain.decide(_frame(observer, dry, builder, created_at=now))
        assert intent is not None
        if position[0] >= 66.0:
            break
        direction = intent.movement.direction
        position = (position[0] + direction[0] * 0.7, position[1] + direction[1] * 0.7, WADE_Z)
        westmost = min(westmost, position[0])
        now += 0.13
    # The old branch handed the swim to the nearest shore, the islet six
    # cells behind, as soon as its first thirteen-cell route ran out.
    assert westmost >= 19.5 and position[0] >= 66.0, (position, intent.debug_role)


def test_a_dry_identity_swims_at_once_when_the_goal_is_on_another_land_mass():
    world = _with_atlas(_strait())
    dry, swimmer, builder = _soldiers((12.5, 20.5, 234.75))
    other_land, same_land, at_sea = (80.5, 20.5, 234.75), (8.5, 30.5, 234.75), (40.5, 20.5, WADE_Z)
    for observer in (dry, builder):
        brain, state = _brain_with_goal(world, observer, other_land)
        frame = _frame(observer, *(p for p in (dry, swimmer, builder) if p is not observer))
        goal = state.goal
        assert brain._dry_detours_before_swim(observer, state, goal, 100.0) == 0
        assert brain._crosses_water(frame, observer, state, 100.0)
        assert brain._dry_detours_before_swim(
            observer, state, _Goal(("o",), at_sea, "objective", 3.0, True), 100.0) == 0
        # A dry way round may exist on the same land mass: keep searching it.
        near = _Goal(("o",), same_land, "objective", 3.0, True)
        assert brain._dry_detours_before_swim(observer, state, near, 100.0) == 3
        state.goal = near
        assert not brain._crosses_water(frame, observer, state, 100.0)
        # ... until the only dry route has been rejected as absurdly long.
        state.corridor_rejected_goal, state.corridor_rejected_until = same_land, 145.0
        assert brain._dry_detours_before_swim(observer, state, near, 100.0) == 0
        assert brain._crosses_water(frame, observer, state, 100.0)
    # Without the atlas the shoreline search keeps its three probes.
    brain, state = _brain_with_goal(_strait(), dry, other_land)
    assert brain._dry_detours_before_swim(dry, state, state.goal, 100.0) == 3


def test_a_dry_identity_on_an_islet_plans_the_swim_instead_of_pacing_the_shore():
    world = _with_atlas(_strait())
    # On the islet's last dry column: no walking step gains on the far shore.
    dry = _player(1, TEAM1, (14.5, 14.5, 234.75), is_bot=True)
    swimmer = _player(2, TEAM1, (8.5, 20.5, 234.75), is_bot=True)
    builder = _player(3, TEAM1, (14.5, 26.5, 234.75), is_bot=True)
    for observer in (dry, builder):
        brain, state = _brain_with_goal(world, observer, (80.5, observer.position[1], 234.75))
        others = tuple(p for p in (dry, swimmer, builder) if p is not observer)
        intent = brain._navigation_intent(
            _frame(observer, *others), observer, state, state.goal, 100.0)
        assert state.dry_detour_goal is None and state.dry_route_failures == 0
        assert any(step.affordance is MovementAffordance.SWIM for step in state.route), state.route
        assert intent.movement.direction[0] > 0.5


def _run(**options):
    result = asyncio.run(run_scenario(scenario_args(map="SpookyMansion", **options)))
    return result, json.dumps(result, indent=1, default=str)


@pytest.mark.parametrize("seed", (7, 11))
def test_zombies_on_spooky_mansions_islets_reach_the_mainland(seed):
    result, detail = _run(scenario="islands", zombies=8, survivors=2, seconds=50.0, seed=seed)
    assert result["never_reached_mainland"] == [], detail
    assert max(result["reached_mainland_s"].values()) <= 40.0, detail


def test_zombies_rising_from_spooky_mansions_sea_ring_reach_the_mainland():
    result, detail = _run(scenario="sea", zombies=8, survivors=2, seconds=25.0, seed=7)
    assert result["never_reached_mainland"] == [], detail
    assert max(result["reached_mainland_s"].values()) <= 20.0, detail


def test_zombies_infect_survivors_standing_in_open_water():
    result, detail = _run(scenario="wade", zombies=6, survivors=2, seconds=40.0, seed=7)
    assert result["kills"] == 2, detail
    assert result["all_infected_s"] <= 30.0, detail


def test_zombies_swim_out_to_survivors_on_an_offshore_islet():
    result, detail = _run(scenario="offshore", zombies=8, survivors=2, seconds=120.0, seed=11)
    assert result["kills"] == 2, detail


def _recovering_zombie(world, position=(30.5, 20.5, WADE_Z)):
    """A wading zombie whose swim has been handed to bank recovery."""

    brain = SimpleBotBrain(world)
    observer = _zombie(1, position, wade=True, grounded=False)
    survivor = _player(9, TEAM2, (80.5, 20.5, 234.75))
    first = brain.decide(_zombie_frame(observer, survivor, created_at=100.0))
    assert first is not None and first.movement.direction[0] > 0.5   # on its way east
    state = brain._states[(observer.player_id, observer.generation)]
    state.water_recovery = True
    return brain, state, observer, survivor


def test_bank_recovery_with_no_bank_to_make_for_gives_the_swim_back():
    world = _strait()
    brain, state, observer, survivor = _recovering_zombie(world)

    class _NoShoreInReach(type(world)):
        """The shared flow finds no bank, as in the middle of SpookyMansion's sea."""

        __slots__ = ()

        def water_step(self, position, preferred_goal=None, **kwargs):
            if preferred_goal is None:
                return None
            return super().water_step(position, preferred_goal=preferred_goal, **kwargs)

        def assisted_water_step(self, position, preferred_goal=None, **kwargs):
            return None

    world.__class__ = _NoShoreInReach
    intent = brain.decide(_zombie_frame(observer, survivor, created_at=100.2))
    assert intent is not None and intent.debug_role != "water_no_route"
    assert intent.movement.direction[0] > 0.5
    assert not state.water_recovery


def test_bank_recovery_that_gets_nowhere_gives_the_swim_back():
    brain, state, observer, survivor = _recovering_zombie(_strait())
    # Recovery makes for the islet behind it; the body does not get there.
    back = brain.decide(_zombie_frame(observer, survivor, created_at=100.2))
    assert back is not None and back.movement.direction[0] < -0.5 and state.water_recovery
    # Five seconds on the four-block window has failed for recovery as well.
    again = brain.decide(_zombie_frame(observer, survivor, created_at=105.0))
    assert again is not None and again.movement.direction[0] > 0.5
    assert not state.water_recovery


def test_a_route_brought_from_land_is_not_finished_in_the_shallows_as_a_water_detour():
    world = _strait()
    brain = SimpleBotBrain(world)
    survivor = _player(9, TEAM2, (80.5, 20.5, 234.75))
    afloat = _zombie(1, (15.5, 20.5, WADE_Z), wade=True, grounded=False)
    first = brain.decide(_zombie_frame(afloat, survivor, created_at=100.0))
    assert first is not None and first.movement.direction[0] > 0.5
    state = brain._states[(afloat.player_id, afloat.generation)]
    # The plan made on the islet a moment ago: along its shore, cell by cell.
    state.route = tuple(RouteStep((15.5, 20.5 + stride, SWIM_Z), MovementAffordance.SWIM)
                        for stride in range(1, 9))
    state.route_index, state.route_topology_version = 0, 1
    state.water_detour = False
    # Leaving the islet, the body hops over its bank for a decision: no water
    # column under it, no bearing to take. The route in hand moves it on.
    on_bank = _zombie(1, (14.6, 20.5, 230.5), wade=False, grounded=False)
    leaving = brain.decide(_zombie_frame(on_bank, survivor, created_at=100.13))
    assert leaving is not None
    # ... but it is not a detour to be finished: afloat, the bearing is asked
    # again, and the swim heads for the prey instead of along the shore.
    again = brain.decide(_zombie_frame(afloat, survivor, created_at=100.26))
    assert again is not None and again.movement.direction[0] > 0.5, again.debug_role
    assert abs(again.movement.direction[1]) < 0.5
