"""Bot schematics: validity against server build rules, block-line plans, co-op.

The library is replayed through the authoritative ``CombatSystem`` support
gate (``_block_supported`` with the same ``pending`` semantics as
``handle_block_line``) and the stock ``cube_line`` generator in all four
rotations. Cooperative tests drive the real ``CooperativeBehavior`` with
snapshot movement and a server-faithful BlockLine fixture; the native
physics gate lives in ``scripts/bot_schematic_build_physics.py``.
"""
from __future__ import annotations

from dataclasses import replace
import math
from types import SimpleNamespace

import pytest
import shared.constants as C
from aoslib.world import cube_line

from server.bot_ai.cooperative_behavior import CooperativeBehavior
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import (
    BotAction, BotActionKind, BotProfile, ObjectiveSnapshot, PerceptionFrame,
)
from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
from server.bot_ai.schematics import (
    MAX_LINE_CELLS, PLAN_REACH, fit, get, library, make_bridge, make_ring, make_stair,
    plan_build, rotation_for_facing, structure_placement, validate_plan,
)
from server.bot_ai.schematics.model import FORWARD, local_to_world, right_of
from server.bot_ai.schematics.planner import (
    VoxelView, body_cells, escapes, line_valid, node_for_position, region_for,
    step_ready, walk_distances,
)
from server.bot_ai.schematics.sites import (
    SchematicSites, find_schematic_site, OPTIONAL_TEAM_COOLDOWN, STEP_MAX_FAILURES,
)
from server.combat_runtime import CombatSystem
from tests.test_bot_project_sites import VoxelFixture, make_world, player

GROUND = 62
FLAT = staticmethod(lambda x, y, z: z >= GROUND)
PROFILE = BotProfile("builder", "normal", .6, .5, .5, .8, .7, .25, .1, 1., 1., .5, .5, 20., .02)


def flat(x, y, z):
    return z >= GROUND


def server_replay(solid, plan):
    """Replay a plan through CombatSystem._block_supported exactly as
    handle_block_line does (cube_line cells, solid cells filtered, pending)."""
    added: set[tuple[int, int, int]] = set()

    def get_solid(x, y, z):
        return (x, y, z) in added or solid(x, y, z)

    fake = SimpleNamespace(server=SimpleNamespace(world_manager=SimpleNamespace(get_solid=get_solid)),
                           _NEIGHBOR_OFFSETS=CombatSystem._NEIGHBOR_OFFSETS)
    for step in plan.steps:
        cells = list(cube_line(*step.start, *step.end))
        assert tuple(cells) == step.cells, "planner cells must equal the client's cube_line"
        assert len(cells) <= CombatSystem.BLOCK_LINE_MAX_CELLS
        pending: set[tuple[int, int, int]] = set()
        for cell in (c for c in cells if not get_solid(*c)):
            assert CombatSystem._block_supported(fake, *cell, pending=pending), (step, cell)
            pending.add(cell)
        added |= pending
    return added


DECK = {name for name, s in library().items() if s.ground_mode == "deck"}


@pytest.mark.parametrize("name", sorted(set(library()) - DECK))
@pytest.mark.parametrize("rotation", range(4))
def test_every_schematic_fits_plans_and_replays_through_server_rules(name, rotation):
    schematic = library()[name]
    placement, why = fit(schematic, flat, (100, 100), GROUND, rotation)
    assert placement is not None, why
    plan = plan_build(flat, placement)
    assert plan.feasible, plan.unbuildable
    assert validate_plan(flat, plan) == []
    built = server_replay(flat, plan)
    assert built == set(placement.cells), "every planned cell covered exactly once"
    assert built.isdisjoint(placement.clear)
    for step in plan.steps:
        assert 1 <= len(step.cells) <= MAX_LINE_CELLS
        assert sum(a != b for a, b in zip(step.start, step.end)) <= 1
        if step.start[2] != step.end[2]:
            assert step.end[2] < step.start[2], "vertical drags go upward"
    # Lines dominate: human-plausible straight runs, not block spam.
    assert plan.line_cells >= .75 * plan.cost
    assert all(0 <= cell[0] < 512 and 0 <= cell[1] < 512 and 1 <= cell[2] <= 238 for cell in built)


@pytest.mark.parametrize("name", sorted(set(library()) - DECK))
def test_rotations_are_quarter_turns_about_the_anchor(name):
    schematic = library()[name]
    base, _ = fit(schematic, flat, (100, 100), GROUND, 0)
    assert base is not None
    for rotation in range(1, 4):
        turned, _ = fit(schematic, flat, (100, 100), GROUND, rotation)
        expected = set()
        for u, f, h, _token in schematic.cells:
            x, y = local_to_world((100, 100), rotation, u, f)
            expected.add((x, y, GROUND - 1 - h))
        assert set(turned.cells) == expected
        assert len(turned.cells) == len(base.cells)
    assert rotation_for_facing(1, 0) == 0 and rotation_for_facing(0, 1) == 1
    assert rotation_for_facing(-1, .2) == 2 and rotation_for_facing(.1, -1) == 3
    for r in range(4):
        fx, fy = FORWARD[r]
        rx, ry = right_of(r)
        assert fx * rx + fy * ry == 0


@pytest.mark.parametrize("name", ["small_hut", "bunker", "pillbox", "vip_shelter", "watchtower", "sniper_nest"])
def test_occupied_structures_leave_their_occupant_a_walkable_exit(name):
    schematic = library()[name]
    placement, _ = fit(schematic, flat, (100, 100), GROUND, 1)
    plan = plan_build(flat, placement)
    view = VoxelView(flat, placement.cells)
    node = node_for_position(view.solid, placement.occupant)
    assert node is not None, "occupant spot is body-clear support"
    region = region_for(placement)
    assert escapes(view.solid, region, node), "door/stair reaches the outside"
    if name == "watchtower":
        assert placement.occupant[2] < GROUND - 5, "platform is climbed via the built stair"
        assert plan.feasible


def test_uneven_ground_is_levelled_with_foundation_first():
    solids = {(x, y, z) for x in range(80, 121) for y in range(80, 121) for z in range(GROUND, GROUND + 4)}
    # A one-block dip (needs fill) and a one-block bump (already solid) under a hut.
    for x in range(99, 102):
        solids.discard((x, 98, GROUND))      # under the u=-2 side wall
    solids.add((99, 102, GROUND - 1))        # bump under the u=+2 side wall
    world = VoxelFixture(solids)
    placement, why = fit(get("small_hut"), world.get_solid, (100, 100), GROUND, 0)
    assert placement is not None, why
    assert placement.foundation and all(cell[2] == GROUND for cell in placement.foundation)
    plan = plan_build(world.get_solid, placement)
    assert plan.feasible and validate_plan(world.get_solid, plan) == []
    foundation_steps = [s.index for s in plan.steps if set(s.cells) & placement.foundation]
    upper_steps = [s.index for s in plan.steps if any(c[2] <= GROUND - 2 for c in s.cells)]
    assert max(foundation_steps) < min(upper_steps)
    # Too steep: a three-deep hole under the footprint is rejected.
    for z in range(GROUND, GROUND + 4):
        solids.discard((100, 102, z))
    assert fit(get("small_hut"), VoxelFixture(solids).get_solid, (100, 100), GROUND, 0)[0] is None


def test_bridge_deck_spans_a_gap_from_the_bank_at_walking_level():
    solids = {(x, y, GROUND) for x in range(80, 121) for y in range(90, 111)}
    for x in range(101, 105):
        for y in range(90, 111):
            solids.discard((x, y, GROUND))
    world = VoxelFixture(solids)
    placement, why = fit(make_bridge(4), world.get_solid, (100, 100), GROUND, 0)
    assert placement is not None, why
    assert set(placement.cells) == {(x, 100, GROUND) for x in range(101, 105)}
    plan = plan_build(world.get_solid, placement)
    assert plan.feasible and len(plan.steps) == 1 and plan.steps[0].start == (101, 100, GROUND)
    assert validate_plan(world.get_solid, plan) == []


def test_unsupportable_structures_are_reported_not_planned():
    floating = structure_placement("floating", ((100, 100, 40), (101, 100, 40)))
    plan = plan_build(flat, floating)
    assert not plan.feasible and set(plan.unbuildable) == {(100, 100, 40), (101, 100, 40)}
    # A free-standing zombie stair: arbitrary cells decompose into lines.
    stair = make_stair(6, 1)
    placement, _ = fit(stair, flat, (100, 100), GROUND, 2)
    plan = plan_build(flat, placement)
    assert plan.feasible and validate_plan(flat, plan) == []
    assert plan.lines >= 5


def test_site_finder_respects_keep_away_bodies_and_reservations():
    world = make_world(thick=True)
    solid = world.solid
    centre = (25.5, 20.5, 17.75)
    choice = find_schematic_site(solid, get("cover_wall"), centre, (1., 0.))
    assert choice is not None and choice.plan.feasible
    blocked = find_schematic_site(solid, get("cover_wall"), centre, (1., 0.),
                                  keep_away=(((25.5, 20.5, 17.75), 30.0),))
    assert blocked is None
    other = find_schematic_site(solid, get("cover_wall"), centre, (1., 0.),
                                reserved=frozenset(choice.placement.cells))
    assert other is None or set(other.placement.cells).isdisjoint(choice.placement.cells)
    body = (25.5, 20.5, 17.75)
    shelter = find_schematic_site(solid, get("vip_shelter"), body, (1., 0.), exact=True,
                                  bodies=(body,), occupant_body=body)
    assert shelter is not None and shelter.placement.occupant[:2] == (25.5, 20.5)
    stranger = (26.5, 18.5, 17.75)  # inside a wall of the shelter
    assert find_schematic_site(solid, get("vip_shelter"), body, (1., 0.), exact=True,
                               any_rotation=False, bodies=(body, stranger), occupant_body=body) is None


def _simulate_claims(builders: int, name: str = "pillbox"):
    world = make_world(thick=True)
    sites = SchematicSites()
    choice = find_schematic_site(world.solid, get(name), (25.5, 20.5, 17.75), (1., 0.), exact=True)
    site = sites.create(2, choice.plan, "defend", (1, 1, 0), 100., optional=False)
    positions = {i: (16.5 + 2 * i, 12.5, 17.75) for i in range(builders)}
    now = 100.
    rounds = 0
    while site.active and rounds < 200:
        rounds += 1
        claims = {}
        for i in range(builders):
            others = [p for j, p in positions.items() if j != i]
            claim = sites.claim(site, (i + 1, 1, 0), positions[i], world.solid, now, others=others)
            if claim is not None:
                claims[i] = claim
        cells = [set(step.cells) for step, _node in claims.values()]
        for a in range(len(cells)):
            for b in range(a + 1, len(cells)):
                assert cells[a].isdisjoint(cells[b]), "two builders never share a line"
        columns = [node[:2] for _step, node in claims.values()]
        assert len(columns) == len(set(columns)), "builders never share a stand"
        for i, (step, node) in claims.items():
            for j, (other, other_node) in claims.items():
                if i != j:
                    assert set(body_cells(other_node)).isdisjoint(step.cells)
        for i, (step, node) in claims.items():
            assert line_valid(world.solid, step.cells)
            for cell in step.cells:
                world._vxl.solids.add(cell)
            sites.step_built(site, step.index, (i + 1, 1, 0), len(step.cells), now)
            positions[i] = (node[0] + .5, node[1] + .5, node[2] - 2.25)
        now += 1.
        sites.refresh(world.solid, now)
    return site, rounds


def test_cooperative_claims_split_the_plan_without_conflicts():
    solo, solo_rounds = _simulate_claims(1)
    team, team_rounds = _simulate_claims(3)
    assert solo.completed_at and team.completed_at
    assert len(team.cells_by_builder) == 3
    assert sum(team.cells_by_builder.values()) == team.plan.cost
    assert team_rounds < solo_rounds, "three builders finish in fewer rounds"


def test_budget_cooldowns_failures_and_fire_abandon_sites():
    world = make_world(thick=True)
    sites = SchematicSites()
    plan = find_schematic_site(world.solid, get("sandbag_wall"), (25.5, 20.5, 17.75), (1., 0.)).plan
    first = sites.create(2, plan, "cover", (1, 1, 0), 100.)
    assert first is not None
    assert not sites.can_start(2, 101.), "optional team cooldown"
    assert sites.can_start(3, 101.), "budgets are per team"
    assert sites.can_start(2, 100. + OPTIONAL_TEAM_COOLDOWN + 1)
    for _ in range(STEP_MAX_FAILURES):
        sites.step_failed(first, 0, (1, 1, 0), "spawn or objective zone", 100.)
    assert 0 in first.skipped
    claim = sites.claim(first, (1, 1, 0), (20.5, 20.5, 17.75), world.solid, 200.)
    assert claim is None or claim[0].index != 0, "a skipped step is never reissued"
    hut = find_schematic_site(world.solid, get("small_hut"), (20.5, 28.5, 17.75), (1., 0.))
    second = sites.create(2, hut.plan, "shelter", (2, 1, 0), 200., optional=False)
    for t in range(4):
        abandoned = sites.under_fire(second, 200. + t)
    assert abandoned and second.abandoned == "under_fire"
    # Idle sites expire.
    sites.refresh(world.solid, 100. + 46.)
    assert first.abandoned == "no_progress"


# --- CooperativeBehavior integration (snapshot movement) ------------------------

def _frame(bot, now, players, objectives, mode="vip"):
    return PerceptionFrame(round(now * 100) + bot.player_id, 1, 1, 1, bot.player_id, bot.generation, now,
                           mode, tuple(players), profile=PROFILE, objectives=tuple(objectives),
                           mode_phase="ACTIVE", behavior_version="cooperative",
                           friendly_mischief=False)


class ServerFixture:
    """Applies BUILD/BUILD_LINE like CombatSystem: cube_line, support, body, stock."""

    def __init__(self, world):
        self.world = world
        self.line_cells = 0
        self.single_cells = 0
        self.rejected = 0
        self.colors: list[str] = []

    def apply(self, bot, action, bodies):
        start = tuple(int(round(v)) for v in action.position)
        end = tuple(int(round(v)) for v in (action.end_position or action.position))
        cells = [c for c in cube_line(*start, *end) if not self.world.solid(*c)]
        eye = bot.eye
        reach_ok = min(math.dist(eye, (c[0] + .5, c[1] + .5, c[2] + .5)) for c in (start, end)) <= 11.6
        body = set()
        for p in bodies:
            top = math.floor(p[2])
            for x in range(math.floor(p[0] - .45), math.floor(p[0] + .45) + 1):
                for y in range(math.floor(p[1] - .45), math.floor(p[1] + .45) + 1):
                    body.update((x, y, z) for z in range(top, top + 3))
        ok = (bool(cells) and reach_ok and bot.blocks >= len(cells)
              and line_valid(self.world.solid, cells) and body.isdisjoint(cells))
        if ok:
            for cell in cells:
                self.world._vxl.solids.add(cell)
            if action.kind is BotActionKind.BUILD_LINE:
                self.line_cells += len(cells)
            else:
                self.single_cells += len(cells)
            self.colors.append(action.argument)
        else:
            self.rejected += 1
        return replace(bot, blocks=bot.blocks - (len(cells) if ok else 0), tool=int(C.BLOCK_TOOL),
                       last_action_kind=action.kind.value, last_action_accepted=ok,
                       last_action_request_id=action.request_id, last_task_accepted=ok,
                       last_action_reason="" if ok else "rejected")


def _run(coordinator, world, bots, strategic, objectives, *, seconds=120., dt=.25, mode="vip",
         until=None):
    server = ServerFixture(world)
    roles: dict[int, list[str]] = {b.player_id: [] for b in bots}
    now = 100.
    for _ in range(int(seconds / dt)):
        for index, bot in enumerate(list(bots)):
            order = coordinator.decide(_frame(bot, now, bots, objectives, mode), bot, None,
                                       strategic(bot, bots))
            if order is None:
                continue
            roles[bot.player_id].append(order.role)
            action = order.action
            if action.kind in {BotActionKind.BUILD_LINE, BotActionKind.BUILD}:
                bots[index] = server.apply(bot, action, [b.position for b in bots])
            elif order.tool_id >= 0:
                bots[index] = replace(bot, tool=order.tool_id)
            elif not order.hold:
                node = node_for_position(world.solid, order.goal)
                if node is not None:
                    # Movement completion: only reachable stands are accepted.
                    region = SimpleNamespace(contains=lambda x, y: True)
                    goal = (node[0] + .5, node[1] + .5, node[2] - 2.25)
                    offset = tuple(bot.eye[a] - bot.position[a] for a in range(3))
                    bots[index] = replace(bot, position=goal,
                                          eye=tuple(goal[a] + offset[a] for a in range(3)))
        now += dt
        if until is not None and until(coordinator, now):
            break
    return bots, server, roles, now


def _vip_setup():
    world = make_world(thick=True)
    vip = player(player_id=1, blocks=150, class_id=int(C.CLASS_SOLDIER),
                 loadout=(int(C.BLOCK_TOOL), int(C.RIFLE_TOOL)), tool=int(C.RIFLE_TOOL),
                 position=(25.5, 20.5, 17.75), eye=(25.5, 20.5, 17.75))
    escort = replace(vip, player_id=2, position=(18.5, 14.5, 17.75), eye=(18.5, 14.5, 17.75))
    escort2 = replace(vip, player_id=3, position=(31.5, 27.5, 17.75), eye=(31.5, 27.5, 17.75))
    return world, [vip, escort, escort2]


def _vip_strategic(bot, bots):
    vip = bots[0]
    if bot.player_id == vip.player_id:
        return ModeBotDecision(bot.position, "vip_rally", sprint=False, arrival_radius=6.,
                               posture=ModeBotPosture.EVASIVE, objective_priority=1.0)
    return ModeBotDecision(vip.position, "vip_guard_formation", arrival_radius=6.,
                           posture=ModeBotPosture.ESCORT, objective_priority=.94)


def _vip_objectives(bots):
    return (ObjectiveSnapshot("vip", 2, bots[0].position, carrier_id=bots[0].player_id),
            ObjectiveSnapshot("team_anchor", 3, (48.5, 20.5, 17.75)),
            ObjectiveSnapshot("team_anchor", 2, (6.5, 6.5, 17.75)))


def test_vip_boxes_himself_in_with_helpers_using_block_lines():
    world, bots = _vip_setup()
    coordinator = CooperativeBehavior(world)

    def done(coord, _now):
        return any(s.purpose == "vip_shelter" and s.completed_at for s in coord.sites.sites.values())

    bots, server, roles, end = _run(coordinator, world, bots, _vip_strategic, _vip_objectives(bots),
                                    seconds=90., until=done)
    shelter = next(s for s in coordinator.sites.sites.values() if s.purpose == "vip_shelter")
    assert shelter.completed_at, shelter.status(world.solid)
    assert all(world.solid(*cell) for cell in shelter.plan.placement.cells)
    assert len(shelter.cells_by_builder) >= 2, "teammates helped the VIP"
    assert shelter.cells_by_builder[1] > 0, "the VIP built from inside"
    assert server.line_cells >= 3 * server.single_cells
    assert server.rejected == 0
    assert any(arg.startswith("rgb:") for arg in server.colors), "palette colours requested"
    # The VIP is inside, with the door open, and now stays put.
    vip = bots[0]
    occupant = shelter.plan.placement.occupant
    assert math.dist(vip.position, occupant) < 1.5
    node = node_for_position(world.solid, vip.position)
    assert escapes(world.solid, region_for(shelter.plan.placement), node)
    order = coordinator.decide(_frame(vip, end + 1, bots, _vip_objectives(bots)), vip, None,
                               _vip_strategic(vip, bots))
    assert order is not None and order.role == "vip_sheltered"
    # Plausible pacing: one drag at a time with tool switch and pauses.
    assert end - 100. > shelter.plan.estimated_seconds * .25


def test_requested_structure_is_built_by_listed_bots_even_with_committed_roles():
    world = make_world(thick=True)
    zombie = player(player_id=4, team=3, blocks=200, loadout=(int(C.BLOCK_TOOL),), tool=int(C.BLOCK_TOOL),
                    position=(14.5, 20.5, 17.75), eye=(14.5, 20.5, 17.75))
    coordinator = CooperativeBehavior(world)
    site = coordinator.request_schematic(3, make_stair(4, 1), (18.5, 20.5, 17.75), 100.,
                                         facing=(1., 0.), builders=(4,), requester="zombie_tower")
    assert site is not None and site.plan.feasible
    status = coordinator.schematic_status(site.site_id)
    assert status["steps"] == len(site.plan.steps) and status["remaining_cells"] == 10

    def strategic(bot, _bots):
        return ModeBotDecision((40.5, 20.5, 17.75), "zombie_hunt", objective_priority=1.0,
                               posture=ModeBotPosture.ASSAULT)

    bots, server, _roles, _end = _run(coordinator, world, [zombie], strategic, (), mode="zom",
                                      seconds=40., until=lambda c, _n: bool(site.completed_at))
    assert site.completed_at and server.rejected == 0
    assert all(world.solid(*cell) for cell in site.plan.placement.cells)
    # Unlisted bots and other teams are not drafted.
    assert coordinator.sites.site_for(2, 4, (18.5, 20.5, 17.75), requested_only=True) is None
    coordinator.cancel_schematic(site.site_id)


def test_infeasible_request_returns_none():
    coordinator = CooperativeBehavior(make_world(thick=True))
    floating = structure_placement("floating", ((30, 20, 5),))
    assert coordinator.request_schematic(2, floating, (30.5, 20.5, 17.75), 100.) is None
    assert coordinator.request_schematic(2, "no_such_schematic", (30.5, 20.5, 17.75), 100.) is None


def test_gateway_applies_requested_palette_colour_and_relays_it():
    relayed = []
    owner = SimpleNamespace(id=1, block_color=0x112233)
    owner.set_color = lambda value: setattr(owner, "block_color", value)
    server = SimpleNamespace(config=None, broadcast=lambda data, **kw: relayed.append(data))
    gateway = BotActionGateway(server)
    gateway._apply_palette(owner, "rgb:c8b27a")
    assert owner.block_color == 0xC8B27A and relayed
    gateway._apply_palette(owner, "")
    gateway._apply_palette(owner, "rgb:zz")
    assert owner.block_color == 0xC8B27A and len(relayed) == 1


def test_defender_holding_a_post_digs_in_with_a_defensive_schematic():
    world = make_world(thick=True)
    defender = player(player_id=5, blocks=200, loadout=(int(C.BLOCK_TOOL), int(C.RIFLE_TOOL)),
                      tool=int(C.RIFLE_TOOL), position=(22.5, 20.5, 17.75), eye=(22.5, 20.5, 17.75))
    coordinator = CooperativeBehavior(world)

    def strategic(bot, _bots):
        return ModeBotDecision((22.5, 20.5, 17.75), "ctf_defend_intel", arrival_radius=4.,
                               posture=ModeBotPosture.DEFEND, objective_priority=.95,
                               watch_position=(45.5, 20.5, 17.75))

    _bots, server, _roles, _end = _run(coordinator, world, [defender], strategic, (), mode="ctf",
                                       seconds=40.)
    built = [s for s in coordinator.sites.sites.values() if s.completed_at]
    assert built and built[0].purpose == "defend"
    assert server.line_cells > server.single_cells and server.rejected == 0
    # It faces the watched approach.
    assert built[0].plan.placement.forward == (1, 0)


def test_occupant_builds_only_from_inside_its_shelter():
    from server.bot_ai.schematics.sites import interior_columns
    world = make_world(thick=True)
    sites = SchematicSites()
    body = (25.5, 20.5, 17.75)
    choice = find_schematic_site(world.solid, get("vip_shelter"), body, (1., 0.), exact=True,
                                 bodies=(body,), occupant_body=body)
    site = sites.create(2, choice.plan, "vip_shelter", (1, 1, 0), 100., optional=False,
                        occupant=(1, 1, 0))
    inside = interior_columns(choice.placement)
    assert len(inside) == 9 and (25, 20) in inside
    position = body
    for _ in range(40):
        claim = sites.claim(site, (1, 1, 0), position, world.solid, 100., prefer_inside=True)
        if claim is None:
            break
        step, node = claim
        assert node[:2] in inside, "the VIP never steps outside its own box"
        for cell in step.cells:
            world._vxl.solids.add(cell)
        sites.step_built(site, step.index, (1, 1, 0), len(step.cells), 100.)
        position = (node[0] + .5, node[1] + .5, node[2] - 2.25)
    assert site.done, "the occupant contributes from inside"


def test_finished_overwatch_post_is_climbed_and_held():
    world = make_world(thick=True)
    sniper = player(player_id=6, blocks=200, loadout=(int(C.BLOCK_TOOL), int(C.SNIPER_TOOL)),
                    tool=int(C.SNIPER_TOOL), position=(18.5, 20.5, 17.75), eye=(18.5, 20.5, 17.75))
    coordinator = CooperativeBehavior(world)
    site = coordinator.request_schematic(2, "watchtower", (24.5, 20.5, 17.75), 100., facing=(1., 0.),
                                         builders=(6,), purpose="overwatch")
    assert site is not None

    def strategic(bot, _bots):
        return ModeBotDecision(bot.position, "fixture_idle", objective_priority=.3)

    bots, _server, roles, _end = _run(coordinator, world, [sniper], strategic, (), mode="tdm",
                                      seconds=40.)
    assert site.completed_at
    occupant = site.plan.placement.occupant
    assert math.dist(bots[0].position, occupant) < 1.0, "climbed the stair onto the platform"
    assert "schematic_climb" in roles[6] and "schematic_overwatch" in roles[6]
    # The climb went tread by tread: every climb goal was a body-clear stand.
    assert node_for_position(world.solid, occupant) is not None



def test_airborne_climber_keeps_its_climb_order():
    world = make_world(thick=True)
    coordinator = CooperativeBehavior(world)
    bot = player(player_id=7, blocks=200, loadout=(int(C.BLOCK_TOOL),), tool=int(C.BLOCK_TOOL),
                 position=(16.5, 20.5, 17.75), eye=(16.5, 20.5, 17.75))
    site = coordinator.request_schematic(2, "stairs", (20.5, 20.5, 17.75), 100., facing=(1., 0.),
                                         builders=(7,))
    order = coordinator.decide(_frame(bot, 100., (bot,), (), "tdm"), bot, None,
                               ModeBotDecision(bot.position, "fixture_idle", objective_priority=.3))
    assert order is not None and order.role.startswith("schematic")
    if order.role == "schematic_approach":
        airborne = replace(bot, grounded=False)
        again = coordinator.decide(_frame(airborne, 100.1, (airborne,), (), "tdm"), airborne, None,
                                   ModeBotDecision(bot.position, "fixture_idle", objective_priority=.3))
        assert again == order, "mid-hop the same movement owner continues"
    assert site.active
