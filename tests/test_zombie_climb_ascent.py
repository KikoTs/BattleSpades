"""Zombie ``climb`` directive: prefab stacks, shared stacks and claw ramps.

An infected bot owns a zombie hand (3x3x3 claw, no block tool), zombie
prefabs and 200 blocks. Its climb destination is a floor to stand on (the
roof beside a survivor's pillar); it gets there by stacking a prefab, by
walking a teammate's stack, or by clawing a ramp into the structure whose
swings never take a floor the ramp still needs. The rig below drives the
real planner and executor with a body that only moves where a step's
footing exists, and a server stand-in that places prefabs and removes
exactly the claw's footprint.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import random

import pytest

import shared.constants as C
from server.bot_ai.messages import BotActionKind, PerceptionFrame, PlayerSnapshot
from server.bot_ai.prefab_ascent import PrefabAscent, ladder_of
from server.bot_ai.prefab_policy import load_bot_prefab_geometry
from server.bot_ai.recovery_skills import (
    AscentGoal,
    ClimbAbilities,
    StepKind,
    body_cells,
    cube_aims_keeping_floors,
    node_of,
    node_position,
    plan_ascent,
    standable,
)
from server.bot_ai.skill_driver import LocomotionSkillDriver, SkillRequest
from server.dig_profiles import DIG_CUBE, melee_dig_positions, navigation_dig_profile

CLAW_TOOL = int(C.ZOMBIEHAND_TOOL)
CLAW = navigation_dig_profile(CLAW_TOOL)
LOADOUT = (CLAW_TOOL, int(C.ZOMBIE_PREFAB_TOOL), int(C.PREFAB_TOOL))
PREFABS = ("prefab_zombiehand", "prefab_zombiebone", "prefab_zombiehead")
FLOOR = 100  # ground surface voxel (z grows downward)
STAND = 2.25
TICK = 0.125


@pytest.fixture(scope="module")
def geometry():
    return {item.name: item for item in load_bot_prefab_geometry(PREFABS)}


class World:
    """Solid set with the worker world's contract."""

    def __init__(self, cells, geometry=None):
        self.cells = set(cells)
        self.prefab_geometry = dict(geometry or {})

    def solid(self, x, y, z):
        if not (0 <= x < 512 and 0 <= y < 512 and 0 <= z < 240):
            return True
        return z >= 239 or (x, y, z) in self.cells

    get_solid = solid

    def block_health(self, x, y, z):
        return 5.0

    def has_line_of_sight(self, origin, target):
        dx, dy, dz = (float(target[i]) - float(origin[i]) for i in range(3))
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        if distance <= 1e-6:
            return True
        steps = max(1, int(math.ceil(distance * 2.0)))
        for index in range(1, steps):
            f = index / steps
            if self.solid(int(math.floor(origin[0] + dx * f)), int(math.floor(origin[1] + dy * f)),
                          int(math.floor(origin[2] + dz * f))):
                return False
        return True


@dataclass
class Site:
    cells: set
    centre: tuple[int, int]
    half: int
    roof: int           # roof support z
    pillar: tuple[int, int, int]   # pillar top support (survivor stands on it)
    destination: tuple[float, float, float]  # body position on the roof beside the pillar
    foot: tuple[float, float, float]         # a body position on the ground at the wall


def building(seed: int, *, height: int = 12, pillar: int = 8, hollow: bool = False,
             blocks_from_wall: int = 3, half: int | None = None) -> Site:
    """A solid (or hollow, two-block walls) box with a 1x1 pillar on its roof.

    The climb directive plans within 14 columns of its destination (farther
    away the bot walks first), so ``half + blocks_from_wall`` stays under 14.
    """

    rng = random.Random(seed)
    cx, cy = rng.randrange(60, 200), rng.randrange(60, 200)
    half = rng.randrange(7, 11) if half is None else int(half)
    cells = {(x, y, z) for x in range(cx - half - 30, cx + half + 31)
             for y in range(cy - half - 30, cy + half + 31) for z in range(FLOOR, FLOOR + 6)}
    roof = FLOOR - height
    for x in range(cx - half, cx + half + 1):
        for y in range(cy - half, cy + half + 1):
            for z in range(roof, FLOOR):
                if hollow and abs(x - cx) < half - 1 and abs(y - cy) < half - 1 and z > roof:
                    continue
                cells.add((x, y, z))
    for z in range(roof - pillar, roof):
        cells.add((cx, cy, z))
    side = rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1)))
    destination = (cx + side[0] + 0.5, cy + side[1] + 0.5, roof - STAND)
    approach = rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1)))
    fx = cx + approach[0] * (half + blocks_from_wall)
    fy = cy + approach[1] * (half + blocks_from_wall)
    return Site(cells, (cx, cy), half, roof, (cx, cy, roof - pillar), destination,
                (fx + 0.5, fy + 0.5, FLOOR - STAND))


@dataclass
class Body:
    pid: int
    position: tuple[float, float, float]
    blocks: int = 200
    life: int = 1
    request_id: int = 0
    accepted: bool = True
    placed: int = 0

    def snapshot(self) -> PlayerSnapshot:
        return PlayerSnapshot(
            player_id=self.pid, generation=1, team=1, class_id=int(C.CLASS_ZOMBIE), alive=True,
            spawned=True, position=self.position, eye=self.position, orientation=(1.0, 0.0, 0.0),
            velocity=(0.0, 0.0, 0.0), health=100, tool=CLAW_TOOL, blocks=self.blocks,
            ammo_clip=0, ammo_reserve=0, is_bot=True, loadout=LOADOUT, prefabs=PREFABS,
            grounded=True, life_id=self.life, last_action_request_id=self.request_id,
            last_action_accepted=self.accepted, last_task_accepted=self.accepted)


class Rig:
    """The worker's climb branch, the server's placement/dig rules, a body."""

    def __init__(self, world: World):
        self.world = world
        self.prefab = PrefabAscent(world)
        self.skills = LocomotionSkillDriver(world)
        self.now = 1000.0
        self.frame_id = 0
        self.placements: list[tuple] = []
        self.swings = 0
        self.roles: list[str] = []

    def frame(self, actor: PlayerSnapshot, *others: PlayerSnapshot) -> PerceptionFrame:
        self.frame_id += 1
        return PerceptionFrame(
            frame_id=self.frame_id, map_epoch=1, mode_epoch=1, topology_version=0,
            observer_id=actor.player_id, observer_generation=1, created_at=self.now,
            mode_id="zom", players=(actor, *others), mode_phase="active")

    # -- the server stand-in -------------------------------------------------

    def place(self, body: Body, action) -> None:
        model = self.world.prefab_geometry[action.argument.lower()]
        yaw = int(round(float(action.yaw) / (math.pi / 2))) & 3
        ax, ay, az = (int(round(v)) for v in action.position)
        cells = {(ax + a, ay + b, az + c) for a, b, c in model.cells_by_yaw[yaw]}
        touches = any(self.world.solid(x + dx, y + dy, z + dz) for x, y, z in cells
                      for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
                                         (0, 0, 1), (0, 0, -1)))
        px, py, pz = (math.floor(v) for v in body.position)
        overlaps = any((px + dx, py + dy, pz + dz) in cells
                       for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (0, 1, 2))
        accepted = touches and not overlaps and model.block_count <= body.blocks and all(
            1 <= z <= 238 for _x, _y, z in cells)
        if accepted:
            self.world.cells |= cells
            body.blocks -= model.block_count
            body.placed += 1
            self.placements.append((action.argument, (ax, ay, az), yaw))
        body.request_id, body.accepted = int(action.request_id), accepted

    def swing(self, body: Body, aim_point) -> None:
        aim = tuple(int(math.floor(v)) for v in aim_point)
        if not self.world.solid(*aim) or math.dist(body.position, aim_point) > 3.6:
            return
        self.swings += 1
        for cell in melee_dig_positions(aim, DIG_CUBE):
            if cell[2] <= 238:
                self.world.cells.discard(cell)

    # -- one decision of the worker's climb branch ----------------------------

    def tick(self, body: Body, destination, others=()) -> str:
        actor = body.snapshot()
        self.now += TICK
        if self.skills.active(actor):
            return self.apply(body, self.skills.step(actor, None, self.now), "continue")
        frame = self.frame(actor, *others)
        step = self.prefab.step(frame, actor, destination,
                                avoided=self.skills.avoided(actor, self.now),
                                failures=self.skills.recent_failures(actor, self.now))
        if step is not None:
            if step.route is not None:
                command = self.skills.adopt(actor, None, step.route, self.now,
                                            purpose="prefab_ascent")
                return self.apply(body, command, step.role)
            if step.action.kind is BotActionKind.PLACE_PREFAB:
                self.place(body, step.action)
            if step.approach is not None:
                # The worker steers toward the ladder's foot; here the body
                # covers most of a block per decision over standable ground.
                ax, ay = step.approach[0] - body.position[0], step.approach[1] - body.position[1]
                span = math.hypot(ax, ay)
                if span > 1e-6:
                    hop = min(span, 0.8)
                    moved = (body.position[0] + ax / span * hop, body.position[1] + ay / span * hop,
                             body.position[2])
                    if standable(self.world, node_of(moved)):
                        body.position = moved
            self.roles.append(step.role)
            return step.role
        if math.hypot(destination[0] - body.position[0],
                      destination[1] - body.position[1]) <= 14.0:
            command = self.skills.request(actor, None, SkillRequest("climb", target=destination),
                                          self.now)
            return self.apply(body, command, "request")
        return "navigate"

    def apply(self, body: Body, command, role: str) -> str:
        if command is None:
            self.roles.append(role + ":none")
            return role + ":none"
        self.roles.append(command.role)
        if command.status != "running":
            return command.status
        slot = self.skills._slot(body.snapshot())
        step = slot.skill.current if slot.skill is not None else None
        if command.action.kind is BotActionKind.MELEE:
            self.swing(body, command.action.position)
            return command.role
        if command.action.kind is not BotActionKind.NONE:
            return command.role
        if step is None:
            return command.role
        moving = math.hypot(command.direction[0], command.direction[1]) > 0.1 or command.jump
        if not moving:
            return command.role
        source = node_position(step.source)
        if command.role.endswith("_align") or (
                step.kind is StepKind.JUMP
                and math.dist(body.position[:2], source[:2]) > 0.35):
            body.position = source
            return command.role
        # The body only arrives where the step's footing really exists.
        if standable(self.world, step.destination) and not any(
                self.world.solid(*cell) for cell in step.dig_cells):
            body.position = node_position(step.destination)
        return command.role


def reached(destination, body: Body) -> bool:
    return AscentGoal.toward(destination, radius=1.5).is_goal(node_of(body.position))


def climb(rig: Rig, body: Body, destination, *, seconds: float = 120.0, others=()) -> bool:
    for _ in range(int(seconds / TICK)):
        rig.tick(body, destination, others)
        node = node_of(body.position)
        assert standable(rig.world, node) or not rig.world.solid(*node), node
        if reached(destination, body):
            return True
    return False


# -- planner contracts ---------------------------------------------------------


def test_toward_goal_is_the_melee_band_around_the_destination_floor():
    goal = AscentGoal.toward((30.5, 30.5, 88 - STAND), radius=1.5)
    assert goal.is_goal((30, 30, 88))        # on the floor itself
    assert goal.is_goal((31, 30, 89))        # one below, beside it
    assert goal.is_goal((31, 31, 85))        # three above (a stack top beside the roof)
    assert not goal.is_goal((31, 30, 84))    # four above: out of claw reach
    assert not goal.is_goal((30, 30, 90))    # two below
    assert not goal.is_goal((32, 30, 88))    # 1.58 blocks away


def test_claw_without_blocks_keeps_every_floor_it_still_needs():
    abilities = ClimbAbilities.from_observer(Body(1, (0.5, 0.5, 97.75)).snapshot())
    assert abilities.dig is not None and abilities.dig.pattern == DIG_CUBE
    assert abilities.keeps_floors and not abilities.can_build
    site = building(5)
    world = World(site.cells)
    cx, cy = site.centre
    # Inside the mass: a stair east from (cx-3, cy, 95) spares both floors and
    # the straight continuation.
    node = (cx - 3, cy, 95)
    required = [(cx - 2, cy, 93), (cx - 2, cy, 92), (cx - 2, cy, 91), (cx - 3, cy, 91)]
    spare = {(cx - 3, cy, 95), (cx - 2, cy, 94), (cx - 1, cy, 93), (cx - 1, cy, 92)}
    aims = cube_aims_keeping_floors(world, StepKind.STAIR, node, 1, 0, required, spare)
    assert aims is not None and len(aims) == 1
    footprint = set(melee_dig_positions(aims[0], DIG_CUBE))
    assert footprint.issuperset(required) and not footprint & spare


@pytest.mark.parametrize("seed", (1, 2, 3))
@pytest.mark.parametrize("hollow", (False, True))
def test_claw_ramp_plan_never_removes_a_floor_it_later_stands_on(seed, hollow):
    site = building(seed, hollow=hollow)
    world = World(site.cells)
    abilities = ClimbAbilities(CLAW, 0, False)
    goal = AscentGoal.toward(site.destination, radius=1.5)
    plan = plan_ascent(world, node_of(site.foot), goal, abilities, radius=24,
                       max_expansions=2500, time_budget=2.0)
    assert plan is not None, (seed, hollow)
    assert plan.blocks == 0 and goal.is_goal(plan.destination)
    # Replay: every swing removes its cube; every floor must still be there.
    replayed = World(site.cells)
    for step in plan.steps:
        for aim in step.aims:
            if replayed.solid(*aim):
                for cell in melee_dig_positions(aim, DIG_CUBE):
                    if cell[2] <= 238:
                        replayed.cells.discard(cell)
        assert not any(replayed.solid(*cell) for cell in step.dig_cells), step
        assert replayed.solid(*step.destination), ("floor removed by an earlier swing", step)
        assert standable(replayed, step.destination), step
        if step.kind is StepKind.JUMP:
            assert step.source[2] - step.destination[2] == 2


# -- the climb, end to end ------------------------------------------------------


def settle(rig: Rig, body: Body, destination, ticks: int = 8) -> None:
    """A few more decisions so a plan whose last step arrived reports done."""

    for _ in range(ticks):
        rig.tick(body, destination)


@pytest.mark.parametrize("seed", (11, 12))
def test_zombie_with_a_hand_prefab_stacks_then_claws_its_way_to_the_roof(seed, geometry):
    # Seven blocks from the wall there is room to stand a hand against it:
    # its palm, six up and touching the wall, is where the claw ramp starts.
    site = building(seed, half=5, blocks_from_wall=7)
    rig = Rig(World(site.cells, geometry))
    body = Body(1, site.foot, blocks=200)
    assert climb(rig, body, site.destination), (rig.roles[-40:], rig.skills.metrics,
                                                rig.prefab.metrics)
    assert body.placed == 1, rig.prefab.metrics   # one hand fits a 200-block wallet
    assert body.blocks == 200 - geometry["prefab_zombiehand"].block_count
    assert rig.prefab.metrics.get("routes_committed", 0) >= 1
    assert rig.swings >= 1                         # the rest of the way is clawed
    assert rig.skills.metrics.get("prefab_ascent_done", 0) >= 1
    assert rig.skills.metrics.get("climb_start", 0) >= 1
    # The pillar still stands: the climb attacked the roof, not the survivor's support.
    px, py, pz = site.pillar
    assert rig.world.solid(px, py, pz)


def test_the_hand_is_a_seven_move_ladder_to_a_nine_high_thumb_tip(geometry):
    # Measured once per orientation: the hand rests on its wrist; a body on
    # the ground behind the wrist jumps onto it (2), jumps again (4), steps
    # onto the palm's edge (5) and the palm (6), onto the thumb's base (7)
    # and jumps onto the thumb tip (9). The palm is the wide landing.
    hand = geometry["prefab_zombiehand"]
    ladder = ladder_of(hand, 0)
    assert ladder.top == 9
    rises = {item.rise for item in ladder.landings}
    assert rises == {2, 4, 5, 6, 7, 8, 9}        # nothing stands three up: no 1:1 stair
    thumb = ladder.landings[0]
    assert thumb.cell == (6, 0, 0) and thumb.moves == 7 and thumb.entry == (8, 5)
    assert thumb.cost == pytest.approx(2 * 0.9 + 0.27 + 0.45 * 3 + 0.9, abs=0.05)
    assert sum(1 for item in ladder.landings if item.rise == 6) >= 20
    # Every orientation is the same ladder turned; the bone (a flat slab,
    # three thick) has no native way up at all.
    for yaw in range(1, 4):
        turned = ladder_of(hand, yaw)
        assert turned.top == 9 and turned.landings[0].moves == 7
    assert ladder_of(geometry["prefab_zombiebone"], 0).landings == ()


def test_three_blocks_from_the_wall_the_hand_stands_flush_along_it(geometry):
    # Right at the wall there is no room to stand a hand in front of the
    # body, but one fits along the wall with its thumb against the face: the
    # climber walks a few blocks to the wrist's back, up the ladder, and
    # claws the last three stairs from the thumb tip into the roof.
    site = building(12, blocks_from_wall=3)
    rig = Rig(World(site.cells, geometry))
    body = Body(1, site.foot, blocks=200)
    assert climb(rig, body, site.destination), (rig.roles[-40:], rig.skills.metrics,
                                                rig.prefab.metrics)
    assert body.placed == 1 and body.blocks == 65, rig.placements
    cx, cy = site.centre
    tips = [cell for (team, cell) in rig.prefab.landings if cell[2] == FLOOR - 9]
    assert tips, rig.prefab.landings
    # Some nine-high landing stands beside a wall voxel at the level of the
    # stair floor that is dug next: the claw ramp starts there.
    assert any(rig.world.solid(x + dx, y + dy, z - 1) and abs(x - cx) <= site.half + 1
               and abs(y - cy) <= site.half + 1
               for x, y, z in tips for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
    assert rig.swings >= 1 and rig.skills.metrics.get("climb_start", 0) >= 1


def test_a_stack_that_strands_its_climber_is_not_built(geometry):
    # A slab floating twelve up with nothing beside it: a hand's thumb tip
    # (nine up) has no face to claw into and no floor to step onto, so no
    # stack is placed at all, and the body is never left somewhere it cannot
    # stand.
    rng = random.Random(61)
    cx, cy = rng.randrange(60, 200), rng.randrange(60, 200)
    cells = {(x, y, z) for x in range(cx - 30, cx + 31) for y in range(cy - 30, cy + 31)
             for z in range(FLOOR, FLOOR + 6)}
    slab = FLOOR - 12
    for x in range(cx - 2, cx + 3):
        for y in range(cy - 2, cy + 3):
            cells.add((x, y, slab))
    destination = (cx + 0.5, cy + 0.5, slab - STAND)
    rig = Rig(World(cells, geometry))
    body = Body(1, (cx + 4.5, cy + 0.5, FLOOR - STAND), blocks=200)
    assert not climb(rig, body, destination, seconds=30.0)
    assert body.placed == 0 and not rig.placements, rig.prefab.metrics
    assert rig.prefab.metrics.get("no_connected_ascent", 0) >= 1, rig.prefab.metrics
    assert rig.prefab.metrics.get("plans_found", 0) == 0


def test_a_far_body_walks_to_the_ladder_foot_and_places_on_the_way(geometry):
    # Nine blocks from the wall the best site's footprint is beyond placing
    # reach: the climber approaches the ladder's foot and places the hand as
    # soon as a voxel of it is in reach, then climbs without re-planning.
    site = building(13, half=4, blocks_from_wall=9)
    rig = Rig(World(site.cells, geometry))
    body = Body(1, site.foot, blocks=200)
    assert climb(rig, body, site.destination), (rig.roles[-40:], rig.skills.metrics,
                                                rig.prefab.metrics)
    assert body.placed == 1, rig.placements
    assert "prefab_ascent_approach" in rig.roles or rig.prefab.metrics.get("plans_found", 0) >= 1
    assert rig.prefab.metrics.get("routes_committed", 0) >= 1
    assert rig.prefab.metrics.get("landing_dead_end", 0) == 0


@pytest.mark.parametrize("seed", (71, 72, 73))
def test_planning_never_holds_the_body_still_for_long(seed, geometry):
    # Site ranking is a bounded scan of the columns around the body; the
    # native route check runs only on the best few. No decision streak of
    # planning lasts a second (eight decisions at this rig's cadence).
    site = building(seed, half=5, blocks_from_wall=rng_blocks(seed))
    rig = Rig(World(site.cells, geometry))
    body = Body(1, site.foot, blocks=200)
    assert climb(rig, body, site.destination), (rig.roles[-40:], rig.skills.metrics,
                                                rig.prefab.metrics)
    longest = streak = 0
    for role in rig.roles:
        streak = streak + 1 if role == "prefab_ascent_planning" else 0
        longest = max(longest, streak)
    assert longest <= 8, (longest, rig.roles[:60])
    assert rig.prefab.metrics.get("planning_over_1s", 0) == 0


def rng_blocks(seed: int) -> int:
    return 2 + seed % 6


def test_a_climber_stranded_on_a_stack_climbs_back_down_and_forgets_it(geometry):
    # A hand whose landing leads nowhere (the structure changed, the claw
    # finds no stair): the climber standing on its thumb tip is sent back
    # down the ladder, the rungs are no longer offered to teammates and the
    # stack stays protected from claws only as long as it is remembered.
    rng = random.Random(81)
    cx, cy = rng.randrange(60, 200), rng.randrange(60, 200)
    cells = {(x, y, z) for x in range(cx - 30, cx + 31) for y in range(cy - 30, cy + 31)
             for z in range(FLOOR, FLOOR + 6)}
    world = World(cells, geometry)
    controller = PrefabAscent(world)
    hand = geometry["prefab_zombiehand"]
    anchor = (cx, cy, FLOOR - 1 - 8)
    footprint = frozenset((anchor[0] + a, anchor[1] + b, anchor[2] + c) for a, b, c in hand.cells_by_yaw[0])
    world.cells |= footprint
    ladder = ladder_of(hand, 0)
    thumb = ladder.landings[0]
    rungs = tuple((anchor[0] + l.cell[0], anchor[1] + l.cell[1], anchor[2] + l.cell[2]) for l in ladder.landings)
    entry = (anchor[0] + thumb.entry[0], anchor[1] + thumb.entry[1], FLOOR)
    body = Body(9, node_position(rungs[0]), blocks=65)
    actor = body.snapshot()
    rig = Rig(world)
    from server.bot_ai.prefab_ascent import PrefabAccess
    from server.bot_ai.recovery_skills import AscentPlan
    controller._register(actor, PrefabAccess("prefab_zombiehand", anchor, 0, footprint, AscentPlan((), 0, 0, 0),
                                             None, 135, entry=entry, landing=rungs[0], ladder=rungs), rig.now)
    assert (actor.team, rungs[0]) in controller.landings
    assert controller.protected_cells(actor.team, rig.now) == footprint
    # A destination at this height but far along: the claw found no stair.
    destination = (cx + 12.5, cy + 0.5, FLOOR - 9 - STAND)
    step = controller.step(rig.frame(actor), actor, destination, failures=1, failure_reason="climb_no_plan")
    assert step is not None and step.role == "prefab_ascent_descend", controller.metrics
    assert step.route is not None and step.route.steps
    last = step.route.steps[-1].destination
    assert last[2] == FLOOR and last not in footprint, step.route.steps
    assert standable(world, last)
    assert not any(team == actor.team and cell in footprint for team, cell in controller.landings)
    # Without a claw failure the body on a rung is left to the claw ramp.
    controller._register(actor, PrefabAccess("prefab_zombiehand", anchor, 0, footprint, AscentPlan((), 0, 0, 0),
                                             None, 135, entry=entry, landing=rungs[0], ladder=rungs), rig.now)
    assert controller.step(rig.frame(actor), actor, destination, failures=0, failure_reason="") is None


def test_second_climber_reuses_the_first_stack(geometry):
    site = building(21, half=5, blocks_from_wall=7)
    rig = Rig(World(site.cells, geometry))
    first = Body(1, site.foot, blocks=200)
    assert climb(rig, first, site.destination), rig.roles[-40:]
    settle(rig, first, site.destination)
    assert first.placed == 1
    second = Body(2, site.foot, blocks=200)
    reused_before = rig.prefab.metrics.get("routes_reused", 0)
    assert climb(rig, second, site.destination, others=(first.snapshot(),)), (
        rig.roles[-40:], rig.prefab.metrics)
    assert rig.prefab.metrics.get("routes_reused", 0) > reused_before
    # The shared stack is walked, not rebuilt: the second body keeps its wallet
    # unless it needed a stack of its own higher up.
    assert second.placed <= 1


@pytest.mark.parametrize("hollow", (False, True))
def test_zombie_without_blocks_claws_a_ramp_to_the_roof(hollow, geometry):
    site = building(31, hollow=hollow)
    rig = Rig(World(site.cells, geometry))
    body = Body(3, site.foot, blocks=0)
    assert climb(rig, body, site.destination), (rig.roles[-40:], rig.skills.metrics)
    settle(rig, body, site.destination)
    assert body.placed == 0 and not rig.placements
    assert rig.skills.metrics.get("climb_done", 0) >= 1, rig.skills.metrics
    assert rig.swings >= 6
    assert rig.skills.metrics.get("climb_fail_no_blocks", 0) == 0


def test_destination_that_becomes_solid_or_removed_aborts_without_exceptions(geometry):
    site = building(41)
    rig = Rig(World(site.cells, geometry))
    body = Body(4, site.foot, blocks=0)
    for _ in range(40):
        rig.tick(body, site.destination)
    assert rig.skills.active(body.snapshot())
    dx, dy = int(math.floor(site.destination[0])), int(math.floor(site.destination[1]))
    # The survivor walls his roof in: the destination band fills with blocks
    # (not where a body already is; the server refuses those).
    occupied = set(body_cells(node_of(body.position))) | {node_of(body.position)}
    for x in range(dx - 2, dx + 3):
        for y in range(dy - 2, dy + 3):
            for z in range(site.roof - 4, site.roof):
                if (x, y, z) not in occupied:
                    rig.world.cells.add((x, y, z))
    for _ in range(400):
        rig.tick(body, site.destination)
        node = node_of(body.position)
        assert not any(rig.world.solid(*cell) for cell in body_cells(node)), node
    # ... and then the whole roof falls away under a cut.
    for x in range(site.centre[0] - site.half, site.centre[0] + site.half + 1):
        for y in range(site.centre[1] - site.half, site.centre[1] + site.half + 1):
            rig.world.cells.discard((x, y, site.roof))
    for _ in range(400):
        rig.tick(body, site.destination)
        node = node_of(body.position)
        assert not any(rig.world.solid(*cell) for cell in body_cells(node)), node
    assert rig.skills.recent_failures(body.snapshot(), rig.now) >= 1 or rig.skills.metrics.get(
        "climb_no_plan", 0) >= 1 or rig.skills.metrics.get("climb_done", 0) >= 1
    assert rig.skills.failure_reason(body.snapshot()) or rig.skills.metrics.get(
        "climb_done", 0) >= 1


def test_two_failures_turn_the_stacking_to_another_side(geometry):
    site = building(51)
    world = World(site.cells, geometry)
    controller = PrefabAscent(world)
    body = Body(5, site.foot, blocks=200)
    rig = Rig(world)
    actor = body.snapshot()
    controller.step(rig.frame(actor), actor, site.destination, failures=0)
    slot = controller.slots[(actor.player_id, actor.generation)]
    assert slot.side == 0
    controller.step(rig.frame(actor), actor, site.destination, failures=1)
    assert slot.side == 0
    controller.step(rig.frame(actor), actor, site.destination, failures=2)
    assert slot.side == 1 and controller.metrics.get("side_rotated") == 1
    # The same failures do not turn it again; two more do.
    controller.step(rig.frame(actor), actor, site.destination, failures=2)
    assert slot.side == 1
    controller.step(rig.frame(actor), actor, site.destination, failures=4)
    assert slot.side == 2
    # Another life starts afresh.
    reborn = replace(actor, life_id=2)
    controller.step(rig.frame(reborn), reborn, site.destination, failures=4)
    assert controller.slots[(actor.player_id, actor.generation)].side == 0
