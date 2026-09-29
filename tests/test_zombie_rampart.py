"""Zombie survivors wall their refuge together with grounded BlockLine runs."""

from __future__ import annotations

from dataclasses import replace
import math

import shared.constants as C

from server.bot_ai.cooperative_behavior import CooperativeBehavior
from server.bot_ai.messages import BotActionKind, ObjectiveSnapshot
from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
from server.bot_ai.project_sites import (
    MAX_PROJECT_BUILD_REACH, RAMPART_HALF_WIDTH, RAMPART_HEIGHT,
    find_rampart_segment, rampart_cells,
)
from tests.test_bot_cooperative_projects import frame as tdm_frame, move
from tests.test_bot_project_sites import make_world, player

REFUGE = (20.5, 20.5, 17.75)  # plateau top z=20 in make_world(thick=True)
TOP = 20


def _ring_columns():
    hw = RAMPART_HALF_WIDTH
    return {(x, y) for x in range(20 - hw, 21 + hw) for y in range(20 - hw, 21 + hw)
            if abs(x - 20) == hw or abs(y - 20) == hw}


def _build_ring(world, builder, *, friends=(), limit=60):
    """Lay runs until the planner has nothing left; return the runs in order."""
    runs = []
    for _ in range(limit):
        site = find_rampart_segment(world, builder, REFUGE, friendly_positions=friends)
        if site is None:
            break
        runs.append(site)
        for cell in site.cells:
            world._vxl.solids.add(cell)
    return runs


def test_ring_rises_layer_by_layer_from_grounded_straight_runs():
    world = make_world(thick=True)
    builder = player(blocks=200)
    runs = _build_ring(world, builder)

    assert runs, "a flat refuge must offer wall runs"
    for site in runs:
        assert site.kind == "rampart" and site.tool_id == int(C.BLOCK_TOOL)
        assert 1 <= len(site.cells) <= 9
        xs = {c[0] for c in site.cells}
        ys = {c[1] for c in site.cells}
        zs = {c[2] for c in site.cells}
        assert len(zs) == 1 and (len(xs) == 1 or len(ys) == 1), "one straight BlockLine"
        assert all(support in world._vxl.solids or support[2] >= TOP
                   for support in site.support_cells)
        # Within ordinary reach from the standing spot inside the ring.
        assert all(math.dist(site.approach, (x + .5, y + .5, z + .5)) <= MAX_PROJECT_BUILD_REACH
                   for x, y, z in site.cells)
        assert abs(site.approach[0] - 20.5) < RAMPART_HALF_WIDTH
        assert abs(site.approach[1] - 20.5) < RAMPART_HALF_WIDTH
    layers = [site.cells[0][2] for site in runs]
    assert layers == sorted(layers, reverse=True), "lower layer completes before the next"
    for x, y in _ring_columns():
        for z in range(TOP - RAMPART_HEIGHT, TOP):
            assert (x, y, z) in world._vxl.solids, (x, y, z)
    interior = {(x, y, z) for (x, y, z) in world._vxl.solids
                if z < TOP and abs(x - 20) < RAMPART_HALF_WIDTH and abs(y - 20) < RAMPART_HALF_WIDTH}
    assert not interior
    assert not rampart_cells(world, REFUGE)


def test_runs_split_around_reserved_cells_and_bodies_and_skip_cliffs():
    world = make_world(thick=True)
    # East side drops three blocks: a natural cliff, left open.
    for x in range(24, 51):
        for y in range(5, 36):
            for z in (20, 21, 22):
                world._vxl.solids.discard((x, y, z))
    builder = player(blocks=200)
    first = find_rampart_segment(world, builder, REFUGE)
    assert first is not None
    reserved = frozenset(first.cells)
    second = find_rampart_segment(world, builder, REFUGE, reserved_cells=reserved)
    assert second is not None and reserved.isdisjoint(second.cells)

    friend = (16.5, 20.5, 17.75)  # standing on the west wall line
    runs = _build_ring(world, builder, friends=(friend,))
    assert runs
    assert all((16, 20) != (x, y) for site in runs for x, y, _z in site.cells)
    assert all(x != 24 for site in runs for x, y, _z in site.cells), "cliff side stays open"
    assert any(x == 16 for site in runs for x, y, _z in site.cells), "west side still walled"


def test_survivor_squad_shares_the_ring_and_finishes_each_run():
    world = make_world(thick=True)
    coordinator = CooperativeBehavior(world)
    refuge = ObjectiveSnapshot("zombie_refuge", 2, REFUGE)
    order_for = {}

    def decide(bot, now, *others):
        strategic = ModeBotDecision(REFUGE, "zombie_survivor_refuge", sprint=True,
                                    arrival_radius=2.0, directive="fortify",
                                    posture=ModeBotPosture.BUILD, objective_priority=.86,
                                    engagement_radius=28.0)
        perception = replace(tdm_frame(bot, now, *others), mode_id="zom",
                             mode_phase="active", objectives=(refuge,))
        return coordinator.decide(perception, bot, None, strategic)

    alpha = player(blocks=200)
    bravo = player(player_id=2, blocks=200, position=(21.5, 19.5, 17.75), eye=(21.5, 19.5, 17.75))

    first = decide(alpha, 100., bravo)
    assert first is not None and first.role == "rampart_approach"
    alpha = move(alpha, first.goal)
    execute = decide(alpha, 100.5, bravo)
    assert execute is not None and execute.role == "rampart_execute"
    action = execute.action
    assert action.kind is BotActionKind.BUILD_LINE and action.tool_id == int(C.BLOCK_TOOL)
    site = coordinator.lives[(1, 1)].task.site
    assert action.position == site.cells[0] and action.end_position == site.cells[-1]

    # A teammate plans a different run at the same time (teamwork, no clash).
    second = decide(bravo, 100.6, alpha)
    assert second is not None and second.role.startswith("rampart_")
    other = coordinator.lives[(2, 1)].task.site
    assert frozenset(other.cells).isdisjoint(site.cells)
    assert len([p for p in coordinator.teams.projects.values() if p.kind == "rampart"]) == 2

    # Authoritative acceptance plus the cells appearing in the world finish the run.
    for cell in site.cells:
        world._vxl.solids.add(cell)
    alpha = replace(alpha, last_action_request_id=action.request_id, last_task_accepted=True,
                    last_action_kind=BotActionKind.BUILD_LINE.value, last_action_accepted=True)
    after = decide(alpha, 101., bravo)
    assert after is None or not after.role.startswith("rampart_")
    assert coordinator.lives[(1, 1)].task is None
    assert coordinator.teams.metrics["tasks_completed"] == 1
    assert coordinator.lives[(1, 1)].next_project <= 101. + 2.5 + 1e-9

    # The next evaluation starts the following run on fresh cells.
    again = decide(alpha, 104., bravo)
    assert again is not None and again.role.startswith("rampart_")
    follow = coordinator.lives[(1, 1)].task.site
    assert frozenset(follow.cells).isdisjoint(site.cells)
    assert frozenset(follow.cells).isdisjoint(other.cells)
