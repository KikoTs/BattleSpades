"""Zombie horde strategy: collapse cuts, target spread, watchdog, sieges.

Collapse plans are validated with the server's real floating-structure rule
(``WorldManager.find_unsupported_chunks``): after the planned voxels are
removed, the survivor's support must be part of a falling chunk.
"""

from __future__ import annotations

from dataclasses import replace
import math
import random
from types import SimpleNamespace

import pytest

import shared.constants as C

from server.bot_ai.horde_strategy import (
    HordeCoordinator,
    HordeMember,
    SiegeInfo,
    SurvivorTarget,
    pile_count,
    tunnel_cells,
    wall_line,
)
from server.bot_ai.messages import BotActionKind, ObjectiveSnapshot
from server.bot_ai.policies import ModePolicyMemory, objective_decision_for
from server.bot_ai.simple_worker import _BotState
from server.bot_ai.structure_collapse import (
    approach,
    falls_after,
    isolation,
    plan_collapse,
    support_cells,
    horde_floor,
)
from server.bot_ai.zombie_siege import ZombieSiegeService
from server.config import ServerConfig
from server.world_manager import WorldManager
from tests.test_bot_mode_fixes import _brain, _frame, _player

FLOOR = 230  # ground surface voxel z of the synthetic maps (z grows down)
ZOMBIE_TEAM, SURVIVOR_TEAM = 1, 2


def _ground(cells: set, lo: int = 90, hi: int = 170) -> set:
    for x in range(lo, hi):
        for y in range(lo, hi):
            for z in range(FLOOR, 240):
                cells.add((x, y, z))
    return cells


def _stand(x: float, y: float, floor: int) -> tuple[float, float, float]:
    return (x, y, floor - 2.25)


def _pillar() -> set:
    cells = _ground(set())
    for z in range(215, FLOOR):
        cells.add((128, 128, z))
    return cells


def _sky_platform() -> set:
    cells = _ground(set())
    for x in range(125, 132):
        for y in range(125, 132):
            cells.add((x, y, 210))
    for cx, cy in ((125, 125), (130, 125), (125, 130), (130, 130)):
        for x in (cx, cx + 1):
            for y in (cy, cy + 1):
                for z in range(211, FLOOR):
                    cells.add((x, y, z))
    return cells


def _tower() -> set:
    cells = _ground(set())
    for x in range(127, 130):
        for y in range(127, 130):
            for z in range(200, FLOOR):
                cells.add((x, y, z))
    return cells


def _keep() -> set:
    """A 5x5 solid keep: its core is buried, but at claw height."""

    cells = _ground(set())
    for x in range(126, 131):
        for y in range(126, 131):
            for z in range(198, FLOOR):
                cells.add((x, y, z))
    return cells


def _hill() -> set:
    cells = _ground(set())
    for x in range(100, 160):
        for y in range(100, 160):
            height = 30 - max(abs(x - 130), abs(y - 130))
            for z in range(FLOOR - max(0, height), FLOOR):
                cells.add((x, y, z))
    return cells


def _server_drops(cells: set, cut, support) -> bool:
    """Ask the real WorldManager flood whether the support falls."""

    manager = WorldManager(ServerConfig())
    manager.map = object()
    remaining = set(cells) - set(cut)
    manager.get_solid = lambda x, y, z: (x, y, z) in remaining
    chunks = manager.find_unsupported_chunks(list(cut))
    fallen = {cell for chunk in chunks for cell in chunk}
    return all(cell in fallen for cell in support)


HORDE = [_stand(118.5, 118.5, FLOOR), _stand(140.5, 128.5, FLOOR)]


@pytest.mark.parametrize("name,builder,top,max_sites", [
    ("pillar", _pillar, 215, 1),
    ("sky_platform", _sky_platform, 210, 4),
    ("tower", _tower, 200, 2),
    ("keep", _keep, 198, 6),
])
def test_collapse_cut_drops_elevated_survivor_under_real_server_rule(name, builder, top, max_sites):
    cells = builder()
    solid = lambda x, y, z: (x, y, z) in cells
    survivor = _stand(128.5, 128.5, top)
    assert isolation(solid, survivor, HORDE).isolated
    support = support_cells(solid, survivor)
    assert support and all(cell[2] == top for cell in support)
    floor = horde_floor(solid, survivor, HORDE)
    assert floor == FLOOR
    plan = plan_collapse(solid, support, ground_floor_z=floor)
    assert plan is not None, name
    # The survivor's own floor is never the cut, and every cut voxel is a
    # low one a zombie standing on the ground can claw.
    assert not set(plan.cut) & set(support)
    assert all(FLOOR - 3 <= cell[2] < FLOOR for cell in plan.cut)
    assert 1 <= len(plan.sites) <= max_sites
    assert falls_after(solid, plan.cut, support)
    assert _server_drops(cells, plan.cut, support)
    # One voxel short of the plan, the base still stands.
    if len(plan.cut) > 1:
        assert not _server_drops(cells, plan.cut[1:], support)


def test_sky_platform_cut_takes_all_four_legs():
    cells = _sky_platform()
    solid = lambda x, y, z: (x, y, z) in cells
    support = support_cells(solid, _stand(128.5, 128.5, 210))
    plan = plan_collapse(solid, support, ground_floor_z=FLOOR)
    legs = {(x // 5, y // 5) for x, y, _ in plan.cut}
    assert len(plan.cut) == 16 and len(legs) == 4


def test_natural_hill_is_open_ground_and_has_no_cheap_cut():
    cells = _hill()
    solid = lambda x, y, z: (x, y, z) in cells
    survivor = _stand(130.5, 130.5, FLOOR - 30)
    assert not isolation(solid, survivor, HORDE).isolated
    support = support_cells(solid, survivor)
    assert plan_collapse(solid, support, ground_floor_z=FLOOR) is None


def test_claw_sites_are_exposed_voxels_a_swing_can_hit():
    cells = _tower()
    solid = lambda x, y, z: (x, y, z) in cells
    support = support_cells(solid, _stand(128.5, 128.5, 200))
    plan = plan_collapse(solid, support, ground_floor_z=FLOOR)
    for site in plan.sites:
        x, y, z = site
        assert any(not solid(x + dx, y + dy, z + dz)
                   for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
                                      (0, 0, -1)))


# ----------------------------------------------------------- coordination

def _members(*positions, bots=True):
    return [HordeMember(10 + i, p, is_bot=bots) for i, p in enumerate(positions)]


def test_targets_are_spread_instead_of_everyone_on_the_nearest():
    coordinator = HordeCoordinator()
    survivors = [SurvivorTarget(1, (100.0, 100.0, 40.0)), SurvivorTarget(2, (140.0, 100.0, 40.0))]
    # Six zombies all slightly nearer survivor 1.
    zombies = _members(*[(110.0 + i, 105.0, 40.0) for i in range(6)])
    targets = coordinator.assign_targets(zombies, survivors)
    counts = {sid: list(targets.values()).count(sid) for sid in (1, 2)}
    assert counts[1] >= 2 and counts[2] >= 2
    assert max(counts.values()) <= 4


def test_assignment_has_hysteresis_and_counts_human_hunters():
    coordinator = HordeCoordinator()
    survivors = [SurvivorTarget(1, (100.0, 100.0, 40.0)), SurvivorTarget(2, (130.0, 100.0, 40.0))]
    bot = HordeMember(10, (114.0, 100.0, 40.0))
    first = coordinator.assign_targets([bot], survivors)[10]
    # A tiny move toward the other survivor keeps the old target.
    moved = HordeMember(10, (116.0, 100.0, 40.0))
    assert coordinator.assign_targets([moved], survivors)[10] == first
    # Three human zombies already chasing survivor 1 push the bot to 2.
    humans = [HordeMember(20 + i, (101.0, 100.0 + i, 40.0), is_bot=False) for i in range(3)]
    fresh = HordeCoordinator()
    assert fresh.assign_targets([bot] + humans, survivors)[10] == 2


def _walled_ground():
    cells = _ground(set())
    for y in range(90, 170):          # a wall between hunter and survivor
        for z in range(FLOOR - 4, FLOOR):
            cells.add((120, y, z))
    return cells


def test_a_hunter_held_up_at_a_wall_claws_through_it_without_trying_a_flank_first():
    cells = _walled_ground()
    solid = lambda x, y, z: (x, y, z) in cells
    coordinator = HordeCoordinator()
    survivor = [SurvivorTarget(1, _stand(130.5, 128.5, FLOOR))]
    roles = ["hunt"]
    for tenth in range(0, 400):
        # Its swings land once it has been told to claw.
        zombie = [HordeMember(10, _stand(118.5, 128.5, FLOOR), working=roles[-1] == "tunnel")]
        orders = coordinator.plan(zombie, survivor, {}, tenth / 10.0, solid=solid)
        roles.append(orders[10].role)
    roles = roles[1:]
    # With no analysis of the survivor's surroundings the straight run looks
    # as quick as it is short: it is tried first.
    assert roles[0] == "hunt"
    # Two seconds at the wall without getting closer and the claw comes out:
    # no seven-second wait, no walk to another side of the same wall.
    assert "tunnel" in roles[:30]
    assert "flank" not in roles and roles[-1] == "tunnel"
    tunnel = coordinator.plan(zombie, survivor, {}, 41.0, solid=solid)[10]
    assert tunnel.role == "tunnel"
    assert tunnel.cells and all(cell[0] == 120 for cell in tunnel.cells)


def test_a_claw_that_never_lands_gives_way_to_another_side():
    cells = _walled_ground()
    solid = lambda x, y, z: (x, y, z) in cells
    coordinator = HordeCoordinator()
    survivor = [SurvivorTarget(1, _stand(130.5, 128.5, FLOOR))]
    zombie = [HordeMember(10, _stand(118.5, 128.5, FLOOR))]
    roles = [coordinator.plan(zombie, survivor, {}, tenth / 10.0, solid=solid)[10].role
             for tenth in range(0, 200)]
    assert "tunnel" in roles[:30] and roles[-1] == "flank"
    assert coordinator.metrics.stuck_events >= 2


def test_watchdog_does_not_fire_while_the_chase_covers_ground():
    coordinator = HordeCoordinator()
    for second in range(30):
        x = 100.0 + second * 5.0
        orders = coordinator.plan(
            [HordeMember(10, (x, 100.0, 40.0))],
            [SurvivorTarget(1, (x + 20.0, 100.0, 40.0))], {}, float(second))
        assert orders[10].role == "hunt" and orders[10].stuck_level == 0


def test_elevated_survivor_gets_diggers_at_the_roots_and_a_ring_not_a_pile():
    cells = _sky_platform()
    solid = lambda x, y, z: (x, y, z) in cells
    survivor = SurvivorTarget(1, _stand(128.5, 128.5, 210))
    support = support_cells(solid, survivor.position)
    plan = plan_collapse(solid, support, ground_floor_z=FLOOR)
    siege = {1: SiegeInfo(True, FLOOR, plan, 0.0)}
    # Ten zombies heaped right under the platform.
    zombies = _members(*[_stand(127.5 + (i % 3), 127.5 + (i // 3) % 3, FLOOR) for i in range(10)])
    before = pile_count(survivor.position, [z.position for z in zombies])
    assert before >= 6
    orders = HordeCoordinator().plan(zombies, [survivor], siege, 0.0, solid=solid)
    roles = [o.role for o in orders.values()]
    assert roles.count("dig_root") == len(plan.sites) * 2
    diggers = [o for o in orders.values() if o.role == "dig_root"]
    assert {o.aim for o in diggers} == set(plan.sites)
    assert all(set(o.cells) <= set(plan.cut) for o in diggers)
    # Everyone else spreads on a ring around the base; goals are never under
    # the survivor, so the pile disperses.
    goals = [o.goal for o in orders.values()]
    assert pile_count(survivor.position, goals) == 0
    ring = [o.goal for o in orders.values() if o.role == "surround"]
    assert ring and len({(round(g[0]), round(g[1])) for g in ring}) == len(ring)


def test_elevated_survivor_without_cut_gets_two_climbers():
    survivor = SurvivorTarget(1, _stand(128.5, 128.5, 200))
    zombies = _members(*[_stand(120.5 + i, 120.5, FLOOR) for i in range(6)])
    orders = HordeCoordinator().plan(
        zombies, [survivor], {1: SiegeInfo(True, FLOOR, None, 0.0)}, 0.0)
    roles = [o.role for o in orders.values()]
    assert roles.count("climb") == 2 and roles.count("surround") == 4


def test_dug_cut_voxels_drop_out_of_dig_orders():
    cells = _pillar()
    solid = lambda x, y, z: (x, y, z) in cells
    support = support_cells(solid, _stand(128.5, 128.5, 215))
    plan = plan_collapse(solid, support, ground_floor_z=FLOOR)
    cells.difference_update(plan.cut)
    orders = HordeCoordinator().plan(
        _members(_stand(126.5, 128.5, FLOOR)), [SurvivorTarget(1, _stand(128.5, 128.5, 215))],
        {1: SiegeInfo(True, FLOOR, plan, 0.0)}, 0.0, solid=solid)
    assert all(not solid(*cell) for o in orders.values() for cell in o.cells)


def test_tunnel_cells_follow_the_line_toward_the_target():
    cells = {(105, 100, z) for z in range(30, 45)}
    solid = lambda x, y, z: (x, y, z) in cells
    line = tunnel_cells(solid, (103.5, 100.5, 40.0 - 2.25 + 2.25 - 2.25), (120.0, 100.5, 37.75))
    assert line and all(cell[0] == 105 for cell in line)


# ------------------------------------------------- policy / worker contract

def _zombie(pid, position, loadout=(int(C.ZOMBIEHAND_TOOL),)):
    return _player(pid, ZOMBIE_TEAM, position, class_id=int(C.CLASS_ZOMBIE), loadout=loadout)


def test_policy_follows_horde_orders_and_siege_roles_ignore_distant_fire():
    zombie = _zombie(10, (120.5, 128.5, FLOOR - 2.25))
    survivor = _player(1, SURVIVOR_TEAM, (128.5, 128.5, 207.75), class_id=int(C.CLASS_SOLDIER))
    order = ObjectiveSnapshot("zombie_order", ZOMBIE_TEAM, (126.5, 125.5, FLOOR - 2.25),
                              carrier_id=10, state=2, attacker=1,
                              cells=((125, 125, 229), (126, 125, 229)))
    other = ObjectiveSnapshot("zombie_order", ZOMBIE_TEAM, (0.0, 0.0, 0.0),
                              carrier_id=11, state=4, attacker=1)
    frame = _frame("zom", zombie, survivor, objectives=(order, other))
    decision = objective_decision_for(frame, zombie)
    assert decision.role == "zombie_siege_dig" and decision.directive == "siege"
    assert decision.position == order.position
    assert decision.engagement_radius <= 4.5
    for state, role in ((0, "zombie_hunt_survivor"), (1, "zombie_hunt_flank"),
                        (3, "zombie_siege_climb"), (4, "zombie_siege_surround"),
                        (5, "zombie_hunt_tunnel")):
        decided = objective_decision_for(
            _frame("zom", zombie, survivor, objectives=(replace(order, state=state),)), zombie)
        assert decided.role == role
    # Survivors never act on horde orders.
    survivor_decision = objective_decision_for(frame, survivor)
    assert survivor_decision is None or survivor_decision.role != "zombie_siege_dig"


def test_other_zombies_orders_do_not_reset_this_bots_commitment():
    memory = ModePolicyMemory()
    zombie = _zombie(10, (120.5, 128.5, FLOOR - 2.25))
    survivor = _player(1, SURVIVOR_TEAM, (128.5, 128.5, 207.75), class_id=int(C.CLASS_SOLDIER))
    mine = ObjectiveSnapshot("zombie_order", ZOMBIE_TEAM, (126.5, 125.5, 227.75),
                             carrier_id=10, state=4, attacker=1)
    other = ObjectiveSnapshot("zombie_order", ZOMBIE_TEAM, (0.0, 0.0, 0.0),
                              carrier_id=11, state=4, attacker=1)
    first = _frame("zom", zombie, survivor, objectives=(mine, other), now=100.0)
    memory.decide(first, zombie)
    key = (10, 1)
    since = memory._states[key].role_since
    later = _frame("zom", zombie, survivor, objectives=(mine, replace(other, state=2)), now=101.0)
    memory.decide(later, zombie)
    assert memory._states[key].role_since == since


def test_worker_claws_the_assigned_cut_voxel():
    cut = (121, 128, 229)
    brain = _brain({cut})
    brain._set_goal = lambda *args, **kwargs: None
    eye = (119.5, 128.5, 228.0)
    zombie = replace(_zombie(10, (119.5, 128.5, 227.75)), eye=eye)
    order = ObjectiveSnapshot("zombie_order", ZOMBIE_TEAM, (119.5, 128.5, 227.75),
                              carrier_id=10, state=2, attacker=1, cells=(cut,))
    frame = _frame("zom", zombie, objectives=(order,))
    decision = objective_decision_for(frame, zombie)
    intent = brain._objective_block_work_intent(frame, zombie, _BotState(10, 1, 1), 10.0, decision)
    assert intent is not None
    assert intent.action.kind is BotActionKind.MELEE
    assert intent.action.tool_id == int(C.ZOMBIEHAND_TOOL)
    assert tuple(int(v) for v in intent.action.position) == cut
    assert intent.debug_role == "zombie_siege_dig"


# ------------------------------------------------------ gameplay-thread glue

class _FakeZombieMode:
    def __init__(self, survivors, zombies):
        from modes.zombie import ZombiePhase

        self.phase = ZombiePhase.ACTIVE
        self._s = survivors
        self._z = zombies

    def _living_survivors(self):
        return list(self._s)

    def _zombies(self):
        return list(self._z)


def _actor(pid, position, *, bot=True, loadout=(int(C.ZOMBIEHAND_TOOL),)):
    return SimpleNamespace(id=pid, position=position, alive=True, spawned=True,
                           is_bot=bot, loadout=loadout)


def test_service_publishes_siege_orders_within_budget_and_drops_the_platform():
    cells = _sky_platform()
    world = SimpleNamespace(map_name="synthetic",
                            get_solid=lambda x, y, z: (x, y, z) in cells)
    survivor = _actor(1, _stand(128.5, 128.5, 210), bot=False, loadout=())
    zombies = [_actor(10 + i, _stand(127.5 + i % 3, 127.5 + i // 3, FLOOR)) for i in range(8)]
    mode = _FakeZombieMode([survivor], zombies)
    service = ZombieSiegeService(budget_seconds=0.002)
    objectives = []
    for tick in range(400):
        objectives = service.objectives(mode, world, now=100.0 + tick * 0.1)
        if any(o.state == 2 for o in objectives):
            break
    assert objectives and all(o.kind == "zombie_order" for o in objectives)
    assert {o.carrier_id for o in objectives} == {z.id for z in zombies}
    diggers = [o for o in objectives if o.state == 2]
    assert diggers, "siege plan never landed"
    cut = {cell for o in diggers for cell in o.cells}
    support = support_cells(world.get_solid, survivor.position)
    assert _server_drops(cells, cut, support)
    assert service.stats["plans_found"] >= 1


def test_service_is_silent_outside_the_outbreak_and_survives_errors():
    from modes.zombie import ZombiePhase

    world = SimpleNamespace(map_name="m", get_solid=lambda *a: False)
    mode = _FakeZombieMode([_actor(1, (10.0, 10.0, 10.0), bot=False)],
                           [_actor(10, (20.0, 10.0, 10.0))])
    mode.phase = ZombiePhase.COUNTDOWN
    service = ZombieSiegeService()
    assert service.objectives(mode, world, now=1.0) == []
    mode.phase = ZombiePhase.ACTIVE

    def broken(*_args):
        raise RuntimeError("map gone")

    world.get_solid = broken
    assert service.objectives(mode, world, now=2.0) == []


def test_far_stalled_hunter_never_tunnels_and_flank_spots_avoid_walls():
    cells = _ground(set())
    solid = lambda x, y, z: (x, y, z) in cells
    coordinator = HordeCoordinator()
    survivor = [SurvivorTarget(1, _stand(160.5, 128.5, FLOOR))]
    zombie = [HordeMember(10, _stand(100.5, 128.5, FLOOR))]
    roles = {coordinator.plan(zombie, survivor, {}, float(t), solid=solid)[10].role
             for t in range(60)}
    assert "tunnel" not in roles
    # Bury the whole target neighbourhood in a block: no flank spot exists,
    # so the far hunters go straight in instead of stalling on a wall.
    for x in range(150, 171):
        for y in range(118, 139):
            for z in range(FLOOR - 8, FLOOR):
                if (x, y) != (160, 128):
                    cells.add((x, y, z))
    hunters = _members(*[_stand(100.5, 120.5 + 4 * i, FLOOR) for i in range(5)])
    orders = HordeCoordinator().plan(hunters, survivor, {}, 0.0, solid=solid)
    assert {o.role for o in orders.values()} == {"hunt"}


# ------------------------------------------------ approach flood and choices

def _solid(cells):
    return lambda x, y, z: (x, y, z) in cells


def _block(cells: set, xs, ys, zs) -> set:
    for x in xs:
        for y in ys:
            for z in zs:
                cells.add((x, y, z))
    return cells


def _deck_with_stairs() -> set:
    """A 3-wide deck 8 up on two piers, a staircase at its west end."""

    cells = _ground(set())
    top = FLOOR - 8
    _block(cells, range(120, 137), range(127, 130), [top])
    _block(cells, list(range(120, 123)) + list(range(134, 137)), range(127, 130),
           range(top + 1, FLOOR))
    for step in range(1, 8):
        _block(cells, [120 - step], range(127, 130), range(top + step, FLOOR))
    return cells


def _sealed_room() -> set:
    cells = _ground(set())
    for x in range(125, 132):
        for y in range(125, 132):
            cells.add((x, y, FLOOR - 4))
            if x in (125, 131) or y in (125, 131):
                _block(cells, [x], [y], range(FLOOR - 3, FLOOR))
    return cells


def _pillar_under_a_walkway() -> set:
    """A pillar that a zombie can also drop onto from a deck with far stairs."""

    cells = _ground(set(), 90, 190)
    top = FLOOR - 10
    _block(cells, [128], [128], range(top, FLOOR))
    deck = top - 3
    _block(cells, range(127, 150), (129, 130), [deck])
    _block(cells, [136, 144], [130], range(deck + 1, FLOOR))
    for step in range(1, 13):
        _block(cells, [149 + step], (129, 130), range(deck + step, FLOOR))
    return cells


@pytest.mark.parametrize("name,builder,top", [
    ("pillar", _pillar, 215), ("sky_platform", _sky_platform, 210),
    ("tower", _tower, 200), ("keep", _keep, 198),
])
def test_nothing_walks_onto_a_footing_the_flood_closes_on(name, builder, top):
    cells = builder()
    found = approach(_solid(cells), _stand(128.5, 128.5, top))
    assert found.closed and found.frontier == 0, name
    assert all(cell[2] == top for cell in found.steps)
    assert found.moves_from(_stand(120.5, 120.5, FLOOR)) is None


def test_the_flood_follows_the_planners_jump_and_drop_edges_and_no_further():
    for rise, walks_in in ((1, True), (2, True), (3, False)):
        cells = _block(_ground(set()), range(127, 130), range(127, 130), range(FLOOR - rise, FLOOR))
        found = approach(_solid(cells), _stand(128.5, 128.5, FLOOR - rise))
        assert found.closed is not walks_in, rise
        assert (found.moves_from(_stand(124.5, 128.5, FLOOR)) is not None) is walks_in
    for depth, walks_in in ((3, True), (4, True), (5, False)):
        cells = _ground(set())
        for x in range(126, 131):
            for y in range(126, 131):
                for z in range(FLOOR, FLOOR + depth):
                    cells.discard((x, y, z))
        found = approach(_solid(cells), _stand(128.5, 128.5, FLOOR + depth))
        assert found.closed is not walks_in, depth


def test_the_flood_wades_and_is_bounded_on_open_ground():
    cells = _hill()
    found = approach(_solid(cells), _stand(130.5, 130.5, FLOOR - 30), limit=300)
    assert not found.closed and len(found.steps) == 300 and found.frontier >= 8
    # An islet in a pond: the waterbed (z 239) is waded, not a wall.
    pond = {(x, y, 239) for x in range(100, 160) for y in range(100, 160)}
    _block(pond, range(128, 131), range(128, 131), range(237, 239))
    _block(pond, range(100, 120), range(100, 160), range(237, 239))
    found = approach(_solid(pond), _stand(129.5, 129.5, 237))
    assert not found.closed
    assert found.moves_from((117.5, 129.5, 237 - 2.25)) is not None


def test_the_way_in_leads_a_hunter_under_the_deck_round_to_the_stairs():
    cells = _deck_with_stairs()
    solid = _solid(cells)
    survivor = SurvivorTarget(1, _stand(131.5, 128.5, FLOOR - 8))
    found = approach(solid, survivor.position, limit=1200)
    under = _stand(131.5, 132.5, FLOOR)             # four blocks from him, eight below
    moves = found.moves_from(under)
    assert not found.closed and moves is not None and moves > 25
    siege = {1: SiegeInfo(False, FLOOR, None, 0.0, found, survivor.position)}
    coordinator = HordeCoordinator()
    order = coordinator.plan([HordeMember(10, under)], [survivor], siege, 0.0, solid=solid)[10]
    # Still a hunt, but toward the next stretch of the route: west, to the
    # foot of the stairs, not the spot under his feet.
    assert order.role == "hunt"
    assert order.goal[0] < under[0] - 4.0
    assert pile_count(survivor.position, [order.goal]) == 0
    # On the deck itself the run is straight: the prey is the goal.
    beside = _stand(126.5, 128.5, FLOOR - 8)
    order = coordinator.plan([HordeMember(11, beside)], [survivor], siege, 0.0, solid=solid)[11]
    assert order.role == "hunt" and order.goal == survivor.position


def test_policy_runs_a_hunter_to_the_way_in_and_keeps_its_eyes_on_the_prey():
    zombie = _zombie(10, (131.5, 132.5, FLOOR - 2.25))
    survivor = _player(1, SURVIVOR_TEAM, (131.5, 128.5, FLOOR - 10.25),
                       class_id=int(C.CLASS_SOLDIER))
    via = (121.5, 131.5, FLOOR - 2.25)
    order = ObjectiveSnapshot("zombie_order", ZOMBIE_TEAM, via, carrier_id=10, state=0, attacker=1)
    decision = objective_decision_for(_frame("zom", zombie, survivor, objectives=(order,)), zombie)
    assert decision.role == "zombie_hunt_survivor" and decision.position == via
    assert decision.watch_position == survivor.position and decision.sprint
    direct = replace(order, position=survivor.position)
    decision = objective_decision_for(_frame("zom", zombie, survivor, objectives=(direct,)), zombie)
    assert decision.position == survivor.position


def test_a_group_rushes_the_prey_instead_of_spreading_to_flank_spots():
    solid = _solid(_ground(set()))
    survivor = [SurvivorTarget(1, _stand(128.5, 128.5, FLOOR))]
    zombies = _members(*[_stand(100.5 + 2 * i, 110.5 + i, FLOOR) for i in range(8)])
    orders = HordeCoordinator().plan(zombies, survivor, {}, 0.0, solid=solid)
    assert {o.role for o in orders.values()} == {"hunt"}
    assert {o.goal for o in orders.values()} == {survivor[0].position}


def test_a_sealed_room_on_the_hordes_level_is_clawed_open_at_once_not_besieged():
    cells = _sealed_room()
    solid = _solid(cells)
    survivor = SurvivorTarget(1, _stand(128.5, 128.5, FLOOR))
    found = approach(solid, survivor.position)
    assert found.closed and len(found.steps) == 25
    siege = {1: SiegeInfo(True, FLOOR, None, 0.0, found, survivor.position)}
    zombies = _members(_stand(121.5, 128.5, FLOOR), _stand(128.5, 136.5, FLOOR),
                       _stand(170.5, 128.5, FLOOR))
    coordinator = HordeCoordinator()
    orders = coordinator.plan(zombies, [survivor], siege, 0.0, solid=solid)
    near, also_near, far = (orders[10], orders[11], orders[12])
    # No watchdog wait: the very first order is the claw, at the wall's face.
    assert near.role == also_near.role == "tunnel"
    assert coordinator.metrics.breach_choices == 2 and coordinator.metrics.stuck_events == 0
    for order in (near, also_near):
        assert all(solid(*cell) for cell in order.cells)
        assert math.dist(order.goal[:2], survivor.position[:2]) <= 8.0
    # Out of clawing range the bot simply runs in; nobody rings a ground room.
    assert far.role == "hunt"
    assert not {"surround", "climb", "dig_root"} & {o.role for o in orders.values()}
    # At the wall the order carries the voxels to claw.
    at_wall = _members(_stand(123.5, 128.5, FLOOR))
    order = coordinator.plan(at_wall, [survivor], siege, 1.0, solid=solid)[10]
    assert order.role == "tunnel" and order.cells and all(c[0] == 125 for c in order.cells)


def test_a_wall_is_clawed_only_when_the_way_round_is_the_longer_one():
    cells = _walled_ground()
    solid = _solid(cells)
    survivor = SurvivorTarget(1, _stand(124.5, 128.5, FLOOR))
    hunter = [HordeMember(10, _stand(116.5, 128.5, FLOOR))]
    line = wall_line(solid, hunter[0].position, survivor.position)
    assert (line.walls, line.columns) == (1, 1) and 2.5 <= line.first <= 4.5
    # The flood knows this wall runs on either way as far as it looked:
    # nothing walks round within its bound, so the claw is the quick way.
    found = approach(solid, survivor.position)
    assert not found.closed and found.moves_from(hunter[0].position) is None
    long_way = {1: SiegeInfo(False, FLOOR, None, 0.0, found, survivor.position)}
    order = HordeCoordinator().plan(hunter, [survivor], long_way, 0.0, solid=solid)[10]
    assert order.role == "tunnel"
    # The same wall with a gate three blocks along: walk through it.
    for z in range(FLOOR - 4, FLOOR):
        for y in (131, 132):
            cells.discard((120, y, z))
    found = approach(solid, survivor.position)
    assert found.moves_from(hunter[0].position) <= 16
    gate = {1: SiegeInfo(False, FLOOR, None, 0.0, found, survivor.position)}
    order = HordeCoordinator().plan(hunter, [survivor], gate, 0.0, solid=solid)[10]
    assert order.role == "hunt"


def test_wall_line_follows_terraces_and_refuses_cliffs():
    cells = _ground(set())
    _block(cells, range(124, 140), range(120, 140), [FLOOR - 1])      # a terrace one up
    solid = _solid(cells)
    line = wall_line(solid, _stand(118.5, 128.5, FLOOR), _stand(130.5, 128.5, FLOOR - 1))
    assert line is not None and line.walls == 0
    # A target ten blocks up is no tunnel: digging does not climb through air.
    assert wall_line(solid, _stand(118.5, 128.5, FLOOR), _stand(130.5, 128.5, FLOOR - 10)) is None
    assert wall_line(None, (0.0, 0.0, 0.0), (5.0, 0.0, 0.0)) is None


def test_a_footing_one_jump_up_is_hunted_not_besieged():
    cells = _block(_ground(set()), range(127, 130), range(127, 130), range(FLOOR - 2, FLOOR))
    solid = _solid(cells)
    survivor = SurvivorTarget(1, _stand(128.5, 128.5, FLOOR - 2))
    found = approach(solid, survivor.position)
    siege = {1: SiegeInfo(False, FLOOR, None, 0.0, found, survivor.position)}
    zombies = _members(*[_stand(118.5 + i, 122.5, FLOOR) for i in range(6)])
    orders = HordeCoordinator().plan(zombies, [survivor], siege, 0.0, solid=solid)
    assert {o.role for o in orders.values()} == {"hunt"}


def test_the_cut_is_dug_when_it_is_quicker_than_the_walk_and_not_otherwise():
    cells = _pillar_under_a_walkway()
    solid = _solid(cells)
    top = FLOOR - 10
    survivor = SurvivorTarget(1, _stand(128.5, 128.5, top))
    found = approach(solid, survivor.position, limit=900)
    # Not isolated: a zombie on the deck drops onto the pillar.
    on_deck = _stand(128.5, 129.5, top - 3)
    assert not found.closed and found.moves_from(on_deck) == 1
    plan = plan_collapse(solid, support_cells(solid, survivor.position), ground_floor_z=FLOOR)
    assert plan is not None and plan.cost <= 2 and _server_drops(cells, plan.cut, plan.support)
    siege = {1: SiegeInfo(False, FLOOR, plan, 0.0, found, survivor.position)}
    # The horde at the pillar's foot: the stairs are forty blocks of walking
    # away, the cut is one voxel. Dig, and wait round the foot for the fall.
    foot = _members(*[_stand(124.5 + i, 124.5, FLOOR) for i in range(5)])
    coordinator = HordeCoordinator()
    orders = coordinator.plan(foot, [survivor], siege, 0.0, solid=solid)
    roles = [o.role for o in orders.values()]
    assert roles.count("dig_root") >= 1 and set(roles) <= {"dig_root", "surround"}
    assert coordinator.metrics.collapse_choices == 1
    assert pile_count(survivor.position,
                      [o.goal for o in orders.values() if o.role == "surround"]) == 0
    # A hunter who is already up on the deck beside him just steps off it.
    orders = HordeCoordinator().plan([HordeMember(20, on_deck)], [survivor], siege, 0.0,
                                     solid=solid)
    assert orders[20].role == "hunt"


def test_time_without_progress_turns_a_walk_that_is_not_working_into_a_siege():
    cells = _deck_with_stairs()
    solid = _solid(cells)
    survivor = SurvivorTarget(1, _stand(131.5, 128.5, FLOOR - 8))
    found = approach(solid, survivor.position)
    plan = plan_collapse(solid, support_cells(solid, survivor.position), ground_floor_z=FLOOR)
    assert plan is not None
    siege = {1: SiegeInfo(False, FLOOR, plan, 0.0, found, survivor.position)}
    zombies = _members(*[_stand(124.5 + 2 * i, 133.5, FLOOR) for i in range(6)])
    coordinator = HordeCoordinator()
    first = coordinator.plan(zombies, [survivor], siege, 0.0, solid=solid)
    assert {o.role for o in first.values()} == {"hunt"}       # the stairs are near
    roles = set()
    for tenth in range(1, 120):                              # ... but nobody moves
        roles = {o.role for o in coordinator.plan(
            zombies, [survivor], siege, tenth / 10.0, solid=solid).values()}
        if "dig_root" in roles:
            break
    assert "dig_root" in roles and tenth <= 100


def test_a_survivor_who_has_fallen_is_hunted_at_once_whatever_his_old_footing_was():
    cells = _sky_platform()
    solid = _solid(cells)
    up = _stand(128.5, 128.5, 210)
    plan = plan_collapse(solid, support_cells(solid, up), ground_floor_z=FLOOR)
    siege = {1: SiegeInfo(True, FLOOR, plan, 0.0, approach(solid, up), up)}
    zombies = _members(*[_stand(120.5 + i, 121.5, FLOOR) for i in range(6)])
    coordinator = HordeCoordinator()
    before = coordinator.plan(zombies, [SurvivorTarget(1, up)], siege, 0.0, solid=solid)
    assert "dig_root" in {o.role for o in before.values()}
    # The legs are gone and he is on the ground: the analysis is of a spot
    # twenty blocks above him.
    cells.difference_update(plan.cut)
    down = SurvivorTarget(1, _stand(128.5, 128.5, FLOOR))
    after = coordinator.plan(zombies, [down], siege, 0.2, solid=solid)
    assert {o.role for o in after.values()} == {"hunt"}
    assert {o.goal for o in after.values()} == {down.position}


def test_the_waiting_ring_hugs_the_measured_footprint():
    cells = _sky_platform()
    solid = _solid(cells)
    survivor = SurvivorTarget(1, _stand(128.5, 128.5, 210))
    plan = plan_collapse(solid, support_cells(solid, survivor.position), ground_floor_z=FLOOR)
    found = approach(solid, survivor.position)
    siege = {1: SiegeInfo(True, FLOOR, plan, 0.0, found, survivor.position)}
    zombies = _members(*[_stand(100.5 + i, 100.5, FLOOR) for i in range(16)])
    orders = HordeCoordinator().plan(zombies, [survivor], siege, 0.0, solid=solid)
    ring = [o.goal for o in orders.values() if o.role == "surround"]
    assert ring
    reach = [math.dist(g[:2], survivor.position[:2]) for g in ring]
    # Outside the 7x7 platform (corners 4.2 out), within a stride of it.
    assert min(reach) >= 4.0 and max(reach) <= found.radius + 4.5
    assert pile_count(survivor.position, ring) == 0


def test_the_hordes_floor_is_the_ground_at_the_foot_not_where_far_hunters_run_or_jump():
    cells = _pillar()
    _block(cells, range(90, 112), range(90, 170), range(FLOOR - 4, FLOOR))   # a rise to the west
    solid = _solid(cells)
    survivor = _stand(128.5, 128.5, 215)
    running_in = [_stand(100.5, 120.5 + 2 * i, FLOOR - 4) for i in range(6)]
    assert horde_floor(solid, survivor, running_in) == FLOOR
    # A hunter in mid-jump beside the pillar is still standing on the ground.
    jumping = [(126.5, 128.5, FLOOR - 2.25 - 3.0)]
    assert horde_floor(solid, survivor, jumping) == FLOOR
    plan = plan_collapse(solid, support_cells(solid, survivor),
                         ground_floor_z=horde_floor(solid, survivor, running_in))
    assert plan.cost == 1 and plan.cut[0][2] == FLOOR - 1


def test_service_analyses_only_survivors_who_stay_put_and_plans_only_cuts_worth_having():
    cells = _deck_with_stairs()
    world = SimpleNamespace(map_name="synthetic", get_solid=_solid(cells))
    camper = _actor(1, _stand(131.5, 128.5, FLOOR - 8), bot=False, loadout=())
    runner = _actor(2, _stand(100.5, 100.5, FLOOR), bot=False, loadout=())
    zombies = [_actor(10 + i, _stand(124.5 + 2 * i, 133.5, FLOOR)) for i in range(4)]
    mode = _FakeZombieMode([camper, runner], zombies)
    service = ZombieSiegeService(budget_seconds=0.05)
    for tick in range(20):
        runner.position = _stand(100.5 + tick, 100.5, FLOOR)      # eight blocks a second
        service.objectives(mode, world, now=100.0 + tick * 0.125)
    assert 1 in service._analyses and 2 not in service._analyses
    info = service._analyses[1].info
    assert info.approach is not None and not info.isolated and info.position == camper.position
    # The stairs are right there: no collapse plan is paid for.
    assert service.stats["plans"] == 0
    # The hunters sent up get nowhere: now the cut is worth knowing.
    for tick in range(20, 160):
        service.objectives(mode, world, now=100.0 + tick * 0.125)
    assert service.coordinator.struggling(1)
    assert service.stats["plans"] >= 1 and service._analyses[1].info.plan is not None


def test_service_hands_the_claw_report_to_the_watchdog():
    cells = _walled_ground()
    world = SimpleNamespace(map_name="synthetic", get_solid=_solid(cells))
    survivor = _actor(1, _stand(130.5, 128.5, FLOOR), bot=False, loadout=())
    zombie = _actor(10, _stand(118.5, 128.5, FLOOR))
    mode = _FakeZombieMode([survivor], [zombie])
    idle, digging = ZombieSiegeService(), ZombieSiegeService()
    for tick in range(64):
        now = 100.0 + tick * 0.125
        idle.objectives(mode, world, now=now)
        digging.objectives(mode, world, now=now, working=lambda player_id: player_id == 10)
    assert idle.coordinator.metrics.stuck_events >= 1
    assert digging.coordinator.metrics.stuck_events == 0


# ------------------------------------------------------- the cost of knowing

class _Steps:
    """A budget counter that charges a tenth of a millisecond per reading."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.0001
        return self.now


class _EditedWorld:
    """A world that reports its terrain edits, as the world manager does."""

    map_name = "synthetic"

    def __init__(self, cells):
        self.cells = cells
        self.listeners = {}

    def get_solid(self, x, y, z):
        return (x, y, z) in self.cells

    def subscribe_mutations(self, callback):
        token = len(self.listeners) + 1
        self.listeners[token] = callback
        return token

    def unsubscribe_mutations(self, token):
        self.listeners.pop(token, None)

    def edit(self, cell, solid):
        (self.cells.add if solid else self.cells.discard)(cell)
        for callback in tuple(self.listeners.values()):
            callback(*cell, solid, 0, 1)


def test_the_nearest_explored_floor_is_found_without_reading_every_cell():
    cells = _deck_with_stairs()
    found = approach(_solid(cells), _stand(131.5, 128.5, FLOOR - 8), limit=1200)

    def brute(position, reach):
        floor = int(round(position[2] + 2.25))
        best, best_gap = None, reach ** 2
        for cell in found.steps:
            gap = (cell[0] + 0.5 - position[0]) ** 2 + (cell[1] + 0.5 - position[1]) ** 2
            if abs(cell[2] - floor) <= 2 and gap < best_gap:
                best, best_gap = cell, gap
        return best_gap if best is not None else None

    asked = 0
    for x in range(60, 200, 7):
        for y in range(60, 200, 7):
            for z in (FLOOR, FLOOR - 8):
                position = _stand(x + 0.5, y + 0.5, z)
                near = found.entry_near(position, 24.0)
                expected = brute(position, 24.0)
                if expected is None:
                    assert near is None
                    continue
                asked += 1
                gap = (near[0] + 0.5 - position[0]) ** 2 + (near[1] + 0.5 - position[1]) ** 2
                assert gap == expected
    assert asked > 50
    assert sum(len(v) for v in found.squares.values()) == len(found.steps)


def test_the_whole_snapshot_call_stays_inside_its_millisecond():
    cells = _deck_with_stairs()
    world = SimpleNamespace(map_name="synthetic", get_solid=_solid(cells))
    camper = _actor(1, _stand(131.5, 128.5, FLOOR - 8), bot=False, loadout=())
    zombies = [_actor(10 + i, _stand(124.5 + 2 * i, 133.5, FLOOR)) for i in range(6)]
    mode = _FakeZombieMode([camper], zombies)
    steps = _Steps()
    service = ZombieSiegeService(budget_clock=steps)
    spent, first = [], None
    for tick in range(80):
        before = steps.now
        service.objectives(mode, world, now=100.0 + tick * 0.125)
        spent.append(steps.now - before)
        if first is None and 1 in service._analyses:
            first = tick
    # A flood of over a thousand floors is too much for one call: it takes
    # a dozen or more, and none of them runs past the millisecond by more
    # than the step it was in (plus this counter's own readings).
    assert first is not None and first >= 12
    assert max(spent) <= service.budget_seconds + 0.00035
    assert service._analyses[1].info.approach is not None


def test_a_flood_is_taken_again_only_when_the_ground_under_it_changes():
    cells = _sealed_room()
    world = _EditedWorld(cells)
    survivor = _actor(1, _stand(128.5, 128.5, FLOOR), bot=False, loadout=())
    zombies = [_actor(10, _stand(118.5, 128.5, FLOOR))]
    mode = _FakeZombieMode([survivor], zombies)
    service = ZombieSiegeService(budget_seconds=0.05)

    def run(start, seconds):
        for tick in range(int(seconds * 8)):
            service.objectives(mode, world, now=start + tick * 0.125)
        return service.stats["analyses"]

    assert run(100.0, 10.0) == 1                      # ten seconds, one flood
    taken = service._analyses[1].info.approach
    assert taken.closed and service._analyses[1].info.isolated
    # The hunter moves; who stands where is brought up to date for nothing.
    zombies[0].position = _stand(120.5, 128.5, FLOOR)
    assert run(110.0, 5.0) == 1 and service._analyses[1].info.approach is taken
    assert service._analyses[1].info.analysed_at >= 112.0
    # Digging on the far side of the map is none of this room's business.
    world.edit((300, 300, FLOOR), False)
    assert run(115.0, 5.0) == 1
    # A zombie claws the wall open: the room is flooded again and is closed
    # no longer.
    for z in range(FLOOR - 3, FLOOR):
        world.edit((125, 128, z), False)
    assert run(120.0, 5.0) == 2
    assert not service._analyses[1].info.approach.closed
    # A world that reports nothing is flooded on the old timer.
    plain = SimpleNamespace(map_name="plain", get_solid=_solid(_sealed_room()))
    service = ZombieSiegeService(budget_seconds=0.05)
    for tick in range(80):
        service.objectives(mode, plain, now=200.0 + tick * 0.125)
    assert service.stats["analyses"] >= 3


def test_the_terrain_listener_goes_with_the_round_and_with_a_long_silence():
    world = _EditedWorld(_sealed_room())
    survivor = _actor(1, _stand(128.5, 128.5, FLOOR), bot=False, loadout=())
    mode = _FakeZombieMode([survivor], [_actor(10, _stand(118.5, 128.5, FLOOR))])
    service = ZombieSiegeService(budget_seconds=0.05)
    service.objectives(mode, world, now=100.0)
    assert len(world.listeners) == 1
    service.reset()
    assert not world.listeners
    for tick in range(16):
        service.objectives(mode, world, now=101.0 + tick * 0.125)
    assert len(world.listeners) == 1 and service.stats["analyses"] == 1
    # Another mode runs for a long time: thousands of edits, nobody asking.
    for index in range(5000):
        world.edit((300 + index % 50, 300, FLOOR - 1), index % 2 == 0)
    assert not world.listeners
    # Back in the round, nothing learned before the silence is trusted.
    for tick in range(40):
        service.objectives(mode, world, now=110.0 + tick * 0.125)
    assert len(world.listeners) == 1 and service.stats["analyses"] == 2


def test_the_cut_found_diving_for_the_ground_is_the_least_and_drops_the_footing():
    rng = random.Random(61006)
    planned = 0
    for _ in range(60):
        cells = _ground(set())
        top = FLOOR - rng.randrange(5, 16)
        half = rng.randrange(1, 4)
        _block(cells, range(128 - half, 129 + half), range(128 - half, 129 + half), [top])
        legs = []
        for _ in range(rng.randrange(1, 5)):
            x, y = 128 + rng.randrange(-half, half + 1), 128 + rng.randrange(-half, half + 1)
            legs.append((x, y))
            _block(cells, [x], [y], range(top, FLOOR))
        solid = _solid(cells)
        support = support_cells(solid, _stand(128.5, 128.5, top))
        plan = plan_collapse(solid, support, ground_floor_z=FLOOR)
        if plan is None:
            continue
        planned += 1
        assert _server_drops(cells, plan.cut, plan.support)
        # Thin legs under one slab: no cut is cheaper than one voxel a leg,
        # and none of the voxels chosen can be spared.
        assert len(plan.cut) <= len(set(legs))
        for spared in plan.cut:
            rest = tuple(c for c in plan.cut if c != spared)
            assert not _server_drops(cells, rest, plan.support)
    assert planned >= 40
