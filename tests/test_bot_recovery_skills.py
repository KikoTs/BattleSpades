"""Locomotion skills: climb out of water/pits, pillar up, fast-bridge.

Plans are replayed against the server's own build rules (face support,
body overlap, editable layers) and the recovered dig footprints; the
executor is driven with snapshots the way the worker drives it.
"""

from __future__ import annotations

import asyncio
import math

import pytest

import shared.constants as C
from server.bot_ai.messages import (
    BotActionKind,
    MovementAffordance,
    PlayerSnapshot,
)
from server.bot_ai.recovery_skills import (
    MAX_EDIT_Z,
    WATER_SUPPORT_Z,
    AscentGoal,
    AscentSearch,
    ClimbAbilities,
    LocomotionSkill,
    StepKind,
    body_cells,
    dig_aims,
    estimate_swings,
    face_supported,
    find_gap_bridge,
    node_of,
    plan_ascent,
    plan_fast_bridge,
    plan_pillar_up,
    plan_staircase_up,
    standable,
)
from server.bot_ai.skill_driver import (
    LocomotionSkillDriver,
    SkillRequest,
    swim_seconds_to_shore,
)
from server.dig_profiles import melee_dig_positions, navigation_dig_profile
from server.game_constants import build_z_is_safe

SPADE = navigation_dig_profile(int(C.SPADE_TOOL))
SUPERSPADE = navigation_dig_profile(int(C.SUPERSPADE_TOOL))
PICKAXE = navigation_dig_profile(int(C.PICKAXE_TOOL))
KNIFE = navigation_dig_profile(int(C.KNIFE_TOOL))


class World:
    """Mutable voxel set with the worker's ``solid`` contract."""

    def __init__(self, solids=()):
        self.cells = set(solids)

    def solid(self, x, y, z):
        if not (0 <= x < 512 and 0 <= y < 512 and 0 <= z < 240):
            return True
        return (x, y, z) in self.cells or z >= WATER_SUPPORT_Z


def ground(z=100, xs=range(0, 40), ys=range(0, 40), depth=8):
    return {(x, y, zz) for x in xs for y in ys for zz in range(z, z + depth)}


def snapshot(position, *, grounded=True, wade=False, velocity=(0.0, 0.0, 0.0),
             blocks=50, loadout=(int(C.BLOCK_TOOL), int(C.SPADE_TOOL)), **extra):
    return PlayerSnapshot(
        player_id=3, generation=1, team=0, class_id=0, alive=True, spawned=True,
        position=position, eye=position, orientation=(1.0, 0.0, 0.0), velocity=velocity,
        health=100, tool=int(C.BLOCK_TOOL), blocks=blocks, ammo_clip=0, ammo_reserve=0,
        is_bot=True, loadout=tuple(loadout), grounded=grounded, wade=wade, life_id=1, **extra)


def replay(world: World, plan, abilities: ClimbAbilities):
    """Apply a plan with the server's placement rules; return final node.

    Every dig must hit an editable solid cell, every build must be
    face-supported (BlockBuild/BlockLine client parity), in an editable layer
    and outside the standing body, and every destination must be standable.
    """

    for step in plan.steps:
        protected = {step.source, step.destination}
        required = [cell for cell in step.dig_cells if world.solid(*cell)]
        if required:
            assert abilities.dig is not None
            aims = dig_aims(world, required, protected, abilities.dig)
            assert aims is not None, step
            for aim in aims:
                footprint = melee_dig_positions(aim, abilities.dig.pattern)
                assert not set(footprint) & protected
                for cell in footprint:
                    if cell[2] <= MAX_EDIT_Z:
                        world.cells.discard(cell)
        for cell in step.build_cells:
            if world.solid(*cell):
                continue
            assert build_z_is_safe(cell[2])
            assert face_supported(world, cell), (step, cell)
            if step.kind is not StepKind.PILLAR:
                # Stair floors beside the body are placed at a hop's apex;
                # bridge floors are below the feet. Neither may be a body cell
                # of the node the body stands on while placing.
                assert cell not in body_cells(step.source) or step.kind is StepKind.STAIR
            world.cells.add(cell)
        floor = step.destination
        if not world.solid(*floor) and step.kind is not StepKind.PILLAR:
            # A 3x3x3 swing may take the next floor with it; the executor
            # re-lays it from the blocks that swing refunded.
            assert abilities.can_build and face_supported(world, floor), step
            world.cells.add(floor)
        assert standable(world, step.destination), step
    return plan.steps[-1].destination


# -- geometry ----------------------------------------------------------------


def test_standing_crouched_and_swimming_bodies_map_to_their_support():
    assert node_of((10.5, 10.5, 100 - 2.25)) == (10, 10, 100)
    assert node_of((10.5, 10.5, 100 - 2.25 + 0.9)) == (10, 10, 100)  # crouched
    assert node_of((10.5, 10.5, 236.75)) == (10, 10, WATER_SUPPORT_Z)  # afloat


def test_a_spade_clears_a_stair_step_in_one_swing_without_touching_floors():
    world = World(ground(z=100) | {(11, 10, z) for z in range(90, 100)})
    step = [(11, 10, 96), (11, 10, 97), (11, 10, 98)]
    protected = {(10, 10, 100), (11, 10, 99)}
    aims = dig_aims(world, step, protected, SPADE)
    assert aims == [(11, 10, 97)]
    assert estimate_swings(world, step, protected, SPADE) == 1
    # A pickaxe needs one swing per cell, a knife five per authored voxel.
    assert estimate_swings(world, step, protected, PICKAXE) == 3
    assert estimate_swings(world, step, protected, KNIFE) == 15


def test_a_footprint_that_would_remove_the_floor_is_refused():
    world = World(ground(z=100) | {(11, 10, z) for z in range(90, 100)})
    # Only the cell directly above the floor: a column swing centred there
    # would also take the floor, so the swing is aimed one higher.
    aims = dig_aims(world, [(11, 10, 99)], {(11, 10, 100)}, SPADE)
    assert aims == [(11, 10, 98)]
    assert (11, 10, 100) not in melee_dig_positions(aims[0], SPADE.pattern)
    # With the cell above protected too, there is no safe swing at all.
    assert dig_aims(world, [(11, 10, 99)], {(11, 10, 100), (11, 10, 97)}, SPADE) is None


def test_the_immutable_waterbed_is_never_planned_as_a_dig():
    world = World()
    assert dig_aims(world, [(5, 5, WATER_SUPPORT_Z)], set(), SPADE) is None


# -- planning -------------------------------------------------------------------


def test_pillar_up_places_each_block_under_the_body_with_face_support():
    world = World(ground(z=100))
    abilities = ClimbAbilities(None, 20, True)
    plan = plan_pillar_up(world, (10, 10, 100), 6, abilities)
    assert [step.kind for step in plan.steps] == [StepKind.PILLAR] * 6
    assert [step.build_cells[0] for step in plan.steps] == [(10, 10, 99 - i) for i in range(6)]
    assert replay(world, plan, abilities) == (10, 10, 94)


def test_pillar_up_stops_at_the_wallet():
    world = World(ground(z=100))
    plan = plan_pillar_up(world, (10, 10, 100), 6, ClimbAbilities(None, 2, True))
    assert len(plan.steps) == 2


def test_pillar_up_digs_its_head_room_under_an_overhang():
    world = World(ground(z=100) | {(10, 10, 95)})
    abilities = ClimbAbilities(SPADE, 10, True)
    plan = plan_pillar_up(world, (10, 10, 100), 3, abilities)
    assert any((10, 10, 95) in step.dig_cells for step in plan.steps)
    replay(world, plan, abilities)
    # Without a tool the overhang ends the pillar instead.
    blocked = plan_pillar_up(World(ground(z=100) | {(10, 10, 95)}), (10, 10, 100), 3,
                             ClimbAbilities(None, 10, True))
    assert blocked is None or len(blocked.steps) < 3


def test_a_dug_staircase_rises_one_level_per_step_into_a_wall():
    # A wall 8 high east of the body.
    world = World(ground(z=100) | {(x, y, z) for x in range(12, 30) for y in range(0, 40)
                                   for z in range(92, 100)})
    abilities = ClimbAbilities(SPADE, 0, True)
    plan = plan_staircase_up(world, (11, 10, 100), (1, 0, 0), 5, abilities)
    assert [step.kind for step in plan.steps] == [StepKind.STAIR] * 5
    assert [step.destination for step in plan.steps] == [(12 + i, 10, 99 - i) for i in range(5)]
    assert all(not step.build_cells for step in plan.steps)
    assert replay(world, plan, abilities) == (16, 10, 95)


def test_climbing_out_of_the_sea_onto_a_cliff_top():
    # Sea everywhere (waterbed 239), an island whose cliff rises 12 blocks.
    top = WATER_SUPPORT_Z - 12
    island = {(x, y, z) for x in range(20, 40) for y in range(0, 40) for z in range(top, 239)}

    class Atlas:
        width = 512
        main_region_id = 1

        def __init__(self):
            self.regions = [0] * (512 * 512)
            self.primary_support = [255] * (512 * 512)
            self.layer_count = [0] * (512 * 512)
            for x in range(20, 40):
                for y in range(0, 40):
                    self.regions[y * 512 + x] = 1
                    self.primary_support[y * 512 + x] = top
                    self.layer_count[y * 512 + x] = 1

    for abilities, label in (
        (ClimbAbilities(SUPERSPADE, 0, True), "miner"),
        (ClimbAbilities(KNIFE, 200, True), "soldier"),
        (ClimbAbilities(PICKAXE, 2000, True), "engineer"),
    ):
        world = World(island)
        world._atlas = Atlas()
        start = (18, 10, WATER_SUPPORT_Z)
        goal = AscentGoal.main_ground(world, start, radius=12)
        plan = plan_ascent(world, start, goal, abilities, radius=12)
        assert plan is not None, label
        assert plan.rise >= 11, label  # the top, or the notch just below it
        assert goal.is_goal(plan.destination)
        if label == "miner":
            # Superspade, empty wallet: a dug staircase, blocks only from digging.
            assert sum(step.kind is StepKind.STAIR for step in plan.steps) >= 8
        if label == "soldier":
            # A knife is no digger: the climb is built from blocks.
            assert plan.blocks >= 10
        replay(world, plan, abilities)


def test_no_tool_and_no_blocks_means_no_plan():
    world = World(ground(z=100) | {(x, y, z) for x in range(12, 20) for y in range(0, 40)
                                   for z in range(94, 100)})
    goal = AscentGoal.toward((15.5, 10.5, 94 - 2.25), radius=1.0)
    assert plan_ascent(world, (10, 10, 100), goal, ClimbAbilities(None, 0, True)) is None


def test_the_search_resumes_across_time_slices_to_the_same_plan():
    world = World(ground(z=100) | {(x, y, z) for x in range(12, 30) for y in range(0, 40)
                                   for z in range(90, 100)})
    goal = AscentGoal.toward((25.5, 10.5, 90 - 2.25), radius=1.0)
    abilities = ClimbAbilities(SPADE, 10, True)
    one_shot = plan_ascent(world, (10, 10, 100), goal, abilities, radius=20, time_budget=5.0)
    search = AscentSearch(world, (10, 10, 100), goal, abilities, radius=20)
    slices = 0
    while search.advance(0.0005) == "pending":
        slices += 1
        assert slices < 10_000
    assert search.status == "found"
    assert search.plan.steps == one_shot.steps


def test_a_gap_straight_ahead_is_bridged_with_a_floor_line_and_a_landing():
    world = World(ground(z=100, xs=range(0, 20)) | ground(z=100, xs=range(28, 40)))
    abilities = ClimbAbilities(None, 30, True)
    plan = find_gap_bridge(world, (18.5, 10.5, 100 - 2.25), (35.5, 10.5, 97.75), abilities)
    assert plan is not None
    bridges = [step for step in plan.steps if step.kind is StepKind.BRIDGE]
    assert [step.build_cells[0] for step in bridges] == [(x, 10, 100) for x in range(20, 28)]
    assert plan.steps[-1].destination == (28, 10, 100)
    replay(world, plan, abilities)


def test_gaps_that_cannot_be_bridged_are_not():
    abilities = ClimbAbilities(None, 30, True)
    # Too wide for the 16-cell limit.
    wide = World(ground(z=100, xs=range(0, 20)) | ground(z=100, xs=range(40, 60)))
    assert find_gap_bridge(wide, (18.5, 10.5, 97.75), (50.5, 10.5, 97.75), abilities) is None
    # No landing at all.
    edge = World(ground(z=100, xs=range(0, 20)))
    assert find_gap_bridge(edge, (18.5, 10.5, 97.75), (50.5, 10.5, 97.75), abilities) is None
    # A one-deep dip is walkable terrain, not a gap.
    dip = World(ground(z=100, xs=range(0, 20)) | ground(z=101, xs=range(20, 24))
                | ground(z=100, xs=range(24, 40)))
    assert find_gap_bridge(dip, (18.5, 10.5, 97.75), (30.5, 10.5, 97.75), abilities) is None
    # Not enough blocks.
    poor = World(ground(z=100, xs=range(0, 20)) | ground(z=100, xs=range(28, 40)))
    assert find_gap_bridge(poor, (18.5, 10.5, 97.75), (35.5, 10.5, 97.75),
                           ClimbAbilities(None, 4, True)) is None


def test_fast_bridge_walks_existing_floor_and_lays_the_rest():
    world = World(ground(z=100, xs=range(0, 22)))
    plan = plan_fast_bridge(world, (20, 10, 100), (1, 0, 0), 5, ClimbAbilities(None, 10, True))
    assert [step.kind for step in plan.steps] == [StepKind.WALK] + [StepKind.BRIDGE] * 4


# -- executor ---------------------------------------------------------------------


def _skill(plan, *, skill=0.5):
    return LocomotionSkill(plan, "test", 0.0, identity=7, skill=skill, reaction=0.0,
                           step_started_at=0.0)


def test_pillar_jumps_then_places_the_block_once_the_feet_clear_the_cell():
    world = World(ground(z=100))
    abilities = ClimbAbilities(None, 10, True)
    skill = _skill(plan_pillar_up(world, (10, 10, 100), 2, abilities))
    standing = snapshot((10.5, 10.5, 97.75))
    first = skill.update(standing, world, 1.0, abilities)
    assert first.status == "running" and first.jump
    assert first.action.kind is BotActionKind.NONE
    assert first.look[2] > 99.0  # eyes down at the cell under the feet
    # Rising, feet above the cell (z < 99 - 2.1): place it.
    airborne = snapshot((10.5, 10.5, 96.7), grounded=False, velocity=(0.0, 0.0, -0.2))
    placing = skill.update(airborne, world, 1.4, abilities)
    assert placing.action.kind in (BotActionKind.BUILD, BotActionKind.BUILD_LINE)
    assert tuple(int(v) for v in placing.action.position) == (10, 10, 99)


def test_a_quick_hand_places_two_pillar_blocks_in_one_jump():
    world = World(ground(z=100))
    abilities = ClimbAbilities(None, 10, True)
    skill = _skill(plan_pillar_up(world, (10, 10, 100), 3, abilities), skill=0.9)
    high = snapshot((10.5, 10.5, 95.7), grounded=False, velocity=(0.0, 0.0, -0.1))
    command = skill.update(high, world, 1.0, abilities)
    assert command.action.kind is BotActionKind.BUILD_LINE
    cells = {tuple(int(v) for v in command.action.position),
             tuple(int(v) for v in command.action.end_position)}
    assert cells == {(10, 10, 99), (10, 10, 98)}


def test_staircase_digs_with_the_breach_aim_contract_at_the_stock_cadence():
    world = World(ground(z=100) | {(x, y, z) for x in range(12, 30) for y in range(0, 40)
                                   for z in range(92, 100)})
    abilities = ClimbAbilities(SPADE, 0, True)
    skill = _skill(plan_staircase_up(world, (11, 10, 100), (1, 0, 0), 3, abilities))
    body = snapshot((11.5, 10.5, 97.75), loadout=(int(C.SPADE_TOOL),), blocks=0)
    swing = skill.update(body, world, 1.0, abilities)
    assert swing.action.kind is BotActionKind.MELEE
    assert swing.affordance is MovementAffordance.BREACH  # director checks the ray hit
    assert swing.tool_id == int(C.SPADE_TOOL)
    target = tuple(int(math.floor(v)) for v in swing.action.position)
    assert target in {(12, 10, 97), (11, 10, 96), (12, 10, 96), (12, 10, 98)}
    again = skill.update(body, world, 1.1, abilities)
    assert again.action.kind is BotActionKind.NONE  # 0.8 s spade cadence
    later = skill.update(body, world, 1.0 + SPADE.fire_interval + 0.2, abilities)
    assert later.action.kind is BotActionKind.MELEE


def test_a_stair_floor_beside_the_body_is_placed_at_the_top_of_a_hop():
    world = World(ground(z=100))
    abilities = ClimbAbilities(None, 10, True)
    plan = plan_staircase_up(world, (10, 10, 100), (1, 0, 0), 2, abilities)
    assert plan.steps[0].kind is StepKind.STAIR
    assert plan.steps[0].build_cells == ((11, 10, 99),)
    skill = _skill(plan)
    # Leaning toward the new floor's column: the server would refuse it, so
    # the bot hops and places it while its feet are above the cell.
    leaning = snapshot((10.97, 10.5, 97.75))
    command = skill.update(leaning, world, 1.0, abilities)
    assert command.action.kind is BotActionKind.NONE and command.jump
    apex = snapshot((10.97, 10.5, 96.6), grounded=False, velocity=(0.0, 0.0, -0.05))
    command = skill.update(apex, world, 1.3, abilities)
    assert command.action.kind is BotActionKind.BUILD
    assert tuple(int(v) for v in command.action.position) == (11, 10, 99)


def test_bridge_brakes_at_the_lip_then_lays_a_line():
    world = World(ground(z=100, xs=range(0, 21)) | ground(z=100, xs=range(28, 40)))
    abilities = ClimbAbilities(None, 30, True)
    plan = find_gap_bridge(world, (20.5, 10.5, 97.75), (35.5, 10.5, 97.75), abilities)
    skill = _skill(plan, skill=0.8)
    running = snapshot((20.5, 10.5, 97.75), velocity=(0.24, 0.0, 0.0))
    brake = skill.update(running, world, 1.0, abilities)
    assert brake.direction[0] < 0 and brake.action.kind is BotActionKind.NONE
    stopped = snapshot((20.6, 10.5, 97.75))
    lay = skill.update(stopped, world, 1.5, abilities)
    assert lay.action.kind is BotActionKind.BUILD_LINE
    start = tuple(int(v) for v in lay.action.position)
    end = tuple(int(v) for v in lay.action.end_position)
    assert start == (21, 10, 100) and end[1:] == (10, 100) and 22 <= end[0] <= 25
    # Backwards bridging: the eyes face back the way the body came.
    assert lay.look[0] < stopped.eye[0]


@pytest.mark.parametrize("x,crouches", [(20.3, True), (20.6, False)])
def test_bridge_lays_standing_while_the_body_reaches_into_the_line(x, crouches):
    """The server counts a crouched body down to the floor layer.

    A body at 20.6 is 0.9 wide and reaches into column 21, the line's first
    cell. Crouching there cancelled the accepted line at its commit, without
    a rejection the skill could count: DoubleDragon bot 11 re-laid the same
    line for 17 s.
    """
    world = World(ground(z=100, xs=range(0, 21)) | ground(z=100, xs=range(28, 40)))
    abilities = ClimbAbilities(None, 30, True)
    plan = find_gap_bridge(world, (20.5, 10.5, 97.75), (35.5, 10.5, 97.75), abilities)
    lay = _skill(plan, skill=0.8).update(snapshot((x, 10.5, 97.75)), world, 1.5, abilities)
    assert lay.action.kind is BotActionKind.BUILD_LINE
    assert tuple(int(v) for v in lay.action.position) == (21, 10, 100)
    assert lay.crouch is crouches


# -- driver --------------------------------------------------------------------------


def test_driver_requests_are_idempotent_and_report_done():
    world = World(ground(z=100))
    driver = LocomotionSkillDriver(world)
    body = snapshot((10.5, 10.5, 97.75))
    request = SkillRequest("pillar_up", levels=1)
    first = driver.request(body, None, request, 1.0)
    assert first is not None and first.status == "running"
    assert driver.active(body)
    same = driver.request(body, None, SkillRequest("pillar_up", levels=1), 1.1)
    assert same is not None and driver.active(body)
    world.cells.add((10, 10, 99))
    landed = snapshot((10.5, 10.5, 96.75))
    done = driver.step(landed, None, 2.0)
    assert done.status == "done"
    assert not driver.active(landed)


def test_swim_estimate_ignores_beaches_cut_off_from_the_main_ground():
    class Route:
        def __init__(self, climbable, gx):
            self.climbable, self.goal_x, self.goal_y, self.distance = climbable, gx, 0, 10

    class Atlas:
        width = 512
        main_region_id = 1
        regions = {0: 1, 1: 2}

        def __init__(self, route):
            self.route = route

        def water_route(self, x, y):
            return self.route

    class W:
        pass

    body = snapshot((5.5, 5.5, 236.75), wade=True)
    world = W()
    world._atlas = Atlas(Route(True, 0))
    world._atlas.regions = [1, 2] + [0] * 10
    assert swim_seconds_to_shore(world, body, None) == pytest.approx(10 * 0.34 + 1.5)
    world._atlas = Atlas(Route(True, 1))
    world._atlas.regions = [1, 2] + [0] * 10
    assert swim_seconds_to_shore(world, body, None) == math.inf
    world._atlas = Atlas(Route(False, 0))
    world._atlas.regions = [1, 2] + [0] * 10
    assert swim_seconds_to_shore(world, body, None) == math.inf


# -- native physics + authoritative gateway --------------------------------------------


@pytest.mark.parametrize("scenario,bots,seconds,expect", [
    ("gap", 3, 25.0, 2),
    ("pit", 4, 30.0, 3),
])
def test_scenarios_recover_with_native_physics(scenario, bots, seconds, expect):
    from scripts.bot_recovery_scenarios import run_scenario

    classes = (int(C.CLASS_SOLDIER), int(C.CLASS_SCOUT), int(C.CLASS_MEDIC), int(C.CLASS_MINER))
    result = asyncio.run(run_scenario(scenario, bots=bots, seconds=seconds, seed=7,
                                      classes=classes, gap_width=8, pit_depth=8))
    assert result.recovered >= expect, result
    assert all(outcome.deaths == 0 for outcome in result.bots)


def test_dragon_island_swimmers_climb_back_onto_the_island():
    from scripts.bot_recovery_scenarios import run_scenario

    result = asyncio.run(run_scenario("water", bots=6, seconds=60.0, seed=11))
    assert result.recovered >= 5, [(o.bot_id, o.recovered_at) for o in result.bots]
    assert result.skill_metrics.get("climb_out_start", 0) >= 1
    # Wall-clock guard against planner stalls. Shared CI runners are slower
    # than a desktop (428 ms observed on GitHub ubuntu-24.04), so keep headroom.
    assert result.slowest_decision_ms < 1000.0
