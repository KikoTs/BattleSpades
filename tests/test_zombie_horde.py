"""Zombie horde strategy: collapse cuts, target spread, watchdog, sieges.

Collapse plans are validated with the server's real floating-structure rule
(``WorldManager.find_unsupported_chunks``): after the planned voxels are
removed, the survivor's support must be part of a falling chunk.
"""

from __future__ import annotations

from dataclasses import replace
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
)
from server.bot_ai.messages import BotActionKind, ObjectiveSnapshot
from server.bot_ai.policies import ModePolicyMemory, objective_decision_for
from server.bot_ai.simple_worker import _BotState
from server.bot_ai.structure_collapse import (
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


def test_watchdog_escalates_a_stalled_hunter_to_flank_then_tunnel():
    cells = _ground(set())
    for y in range(90, 170):          # a wall between hunter and survivor
        for z in range(FLOOR - 4, FLOOR):
            cells.add((120, y, z))
    solid = lambda x, y, z: (x, y, z) in cells
    coordinator = HordeCoordinator()
    survivor = [SurvivorTarget(1, _stand(130.5, 128.5, FLOOR))]
    zombie = [HordeMember(10, _stand(118.5, 128.5, FLOOR))]
    roles = []
    for second in range(0, 40, 1):
        orders = coordinator.plan(zombie, survivor, {}, float(second), solid=solid)
        roles.append(orders[10].role)
    assert roles[0] == "hunt"
    assert "flank" in roles and "tunnel" in roles
    tunnel = next(o for o in [coordinator.plan(zombie, survivor, {}, 41.0, solid=solid)[10]])
    assert tunnel.role == "tunnel"
    assert tunnel.cells and all(cell[0] == 120 for cell in tunnel.cells)
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
