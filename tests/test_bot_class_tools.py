"""Bots use what their class carries, under the production cooperative behaviour."""

from dataclasses import replace
import math
from types import SimpleNamespace

import pytest

import shared.constants as C
from server.bot_ai.cooperative_behavior import CooperativeBehavior
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import (
    BotAction, BotActionKind, EntitySnapshot, ObjectiveSnapshot,
)
from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState
from server.projectiles import PROJECTILE_SPECS
from tests.test_simple_bot_tactics import TEAM1, TEAM2, _frame, _player

LANDMINE, RADAR, TURRET = int(C.LANDMINE_TOOL), int(C.RADAR_STATION_TOOL), int(C.ROCKET_TURRET_TOOL)
DYNAMITE, C4, DISGUISE = int(C.DYNAMITE_TOOL), int(C.C4_TOOL), int(C.DISGUISE_TOOL)
CANNON = int(C.SNOWBLOWER_TOOL)
POST = (20.5, 20.5, 97.75)
EAST = (60.5, 20.5, 97.75)


class _Terrain:
    """Flat ground at z=100 with optional extra solid cells."""

    def __init__(self, extra=()):
        self.extra = set(extra)

    def get_solid(self, x, y, z):
        return z >= 100 or (x, y, z) in self.extra

    def set_solid(self, x, y, z, solid):
        (self.extra.add if solid else self.extra.discard)((x, y, z))


def _brain(extra=()):
    world = SimpleVoxelWorld()
    world._vxl = _Terrain(extra)
    world.map_epoch = world.topology_version = 1
    return world, SimpleBotBrain(world)


def _bot(class_id, tools, stock=(), *, position=POST, blocks=40, player_id=1):
    loadout = (int(C.SMG_TOOL), *tools, int(C.PICKAXE_TOOL))
    return replace(
        _player(player_id, TEAM1, position, class_id=int(class_id), is_bot=True,
                loadout=loadout, weapon_tool=int(C.SMG_TOOL)),
        deployable_stock=tuple(stock), blocks=blocks)


def _at(player, position):
    """The same body somewhere else, eyes included."""
    return replace(player, position=position,
                   eye=(position[0], position[1], position[2] - 1.0))


def _cooperative(observer, *others, entities=(), objectives=(), now=100.0):
    return replace(_frame(observer, *others, created_at=now, objectives=objectives),
                   behavior_version="cooperative", entities=tuple(entities))


def _defend(position=POST, watch=EAST, role="ctf_defend_intel"):
    return ModeBotDecision(position, role, posture=ModeBotPosture.DEFEND,
                           arrival_radius=3.0, objective_priority=0.8, watch_position=watch)


def _state(observer):
    return _BotState(1, 1, observer.life_id)


def _own(tool, position, *, owner=1, entity_id=7, radius=5.0, team=TEAM1):
    return EntitySnapshot(entity_id, 0, team, owner, position, kind="deployable",
                          tool_id=tool, blast_radius=radius, hazardous=radius > 0)


# --- mines, turrets, radar ---------------------------------------------------

def test_scout_at_its_post_lays_a_mine_on_the_side_the_enemy_comes_from():
    world, brain = _brain()
    scout = _bot(C.CLASS_SCOUT, (LANDMINE,), ((LANDMINE, 3),))
    intent = brain._class_deploy_intent(_cooperative(scout), scout, _state(scout), 100.0, _defend())
    assert intent.action.kind is BotActionKind.DEPLOY and intent.action.tool_id == LANDMINE
    spot = intent.action.position
    # A few steps out toward the watched approach, on the ground, in reach.
    assert 2.0 <= spot[0] - POST[0] <= 4.5 and abs(spot[1] - POST[1]) < 1.0
    assert math.dist(spot, scout.position) <= float(C.LANDMINE_FAR_RADIUS)
    assert intent.debug_role == f"deploy_{LANDMINE}_post"


def test_no_mine_without_stock_beside_a_friend_on_the_objective_or_past_three():
    world, brain = _brain()
    decision = _defend()
    scout = _bot(C.CLASS_SCOUT, (LANDMINE,), ((LANDMINE, 3),))
    empty = replace(scout, deployable_stock=((LANDMINE, 0),))
    assert brain._class_deploy_intent(_cooperative(empty), empty, _state(empty), 100.0, decision) is None
    # Every candidate spot has a teammate inside the blast.
    crowd = tuple(_player(10 + index, TEAM1, (POST[0] + dx, POST[1] + dy, POST[2]), is_bot=True)
                  for index, (dx, dy) in enumerate(((4, 0), (3, 2), (3, -2), (1, 3), (1, -3))))
    assert brain._class_deploy_intent(
        _cooperative(scout, *crowd), scout, _state(scout), 100.0, decision) is None
    mines = tuple(_own(LANDMINE, (POST[0] - 20 - 9 * index, POST[1], 100.0), entity_id=index)
                  for index in range(3))
    assert brain._class_deploy_intent(
        _cooperative(scout, entities=mines), scout, _state(scout), 100.0, decision) is None
    # Not on top of what the team has to stand on.
    intel = ObjectiveSnapshot("ctf_intel", TEAM1, (POST[0] + 4.0, POST[1], POST[2]))
    intent = brain._class_deploy_intent(
        _cooperative(scout, objectives=(intel,)), scout, _state(scout), 100.0, decision)
    assert intent is None or math.dist(intent.action.position, intel.position) >= 3.0


def test_a_carrier_and_a_bot_still_crossing_the_map_keep_their_gadgets():
    world, brain = _brain()
    scout = _bot(C.CLASS_SCOUT, (LANDMINE,), ((LANDMINE, 3),))
    carrier = replace(scout, carried_entity_id=4)
    assert brain._class_deploy_intent(
        _cooperative(carrier), carrier, _state(carrier), 100.0, _defend()) is None
    assault = ModeBotDecision(EAST, "ctf_capture_intel", posture=ModeBotPosture.ASSAULT,
                              objective_priority=0.9)
    assert brain._class_deploy_intent(
        _cooperative(scout), scout, _state(scout), 100.0, assault) is None
    far_post = _defend(position=(120.5, 20.5, 97.75))
    assert brain._class_deploy_intent(
        _cooperative(scout), scout, _state(scout), 100.0, far_post) is None


def test_an_enemy_just_lost_from_view_gets_a_mine_or_a_turret_left_for_him():
    world, brain = _brain()
    assault = ModeBotDecision(EAST, "ctf_capture_intel", posture=ModeBotPosture.ASSAULT,
                              objective_priority=0.9)
    scout = _bot(C.CLASS_SCOUT, (LANDMINE,), ((LANDMINE, 3),))
    state = _state(scout)
    state.contact_position, state.contact_until = (POST[0], POST[1] + 15.0, POST[2]), 103.0
    intent = brain._class_deploy_intent(_cooperative(scout), scout, state, 100.0, assault)
    assert intent.action.tool_id == LANDMINE and intent.debug_role.endswith("_contact")
    assert intent.action.position[1] - POST[1] >= 2.0  # toward where he went
    engineer = _bot(C.CLASS_ENGINEER, (TURRET,), ((TURRET, 2),))
    state = _state(engineer)
    state.contact_position, state.contact_until = (POST[0], POST[1] + 15.0, POST[2]), 103.0
    intent = brain._class_deploy_intent(_cooperative(engineer), engineer, state, 100.0, assault)
    assert intent.action.tool_id == TURRET and intent.action.position == engineer.position
    assert intent.action.yaw == pytest.approx(math.pi / 2)
    # The sighting is stale: back to the job.
    assert brain._class_deploy_intent(_cooperative(engineer, now=110.0), engineer, _state(engineer),
                                      110.0, assault) is None


def test_defenders_set_up_turret_and_radar_once_each_and_not_on_top_of_a_teammates():
    world, brain = _brain()
    engineer = _bot(C.CLASS_ENGINEER, (TURRET,), ((TURRET, 2),))
    state = _state(engineer)
    first = brain._class_deploy_intent(_cooperative(engineer), engineer, state, 100.0, _defend())
    assert first.action.tool_id == TURRET and first.action.yaw == pytest.approx(0.0)
    assert brain._class_deploy_intent(_cooperative(engineer, now=101.0), engineer, state,
                                      101.0, _defend()) is None  # cooling down
    theirs = _own(TURRET, (POST[0] + 5.0, POST[1], 100.0), owner=9)
    assert brain._class_deploy_intent(_cooperative(engineer, entities=(theirs,)), engineer,
                                      _state(engineer), 100.0, _defend()) is None
    # An enemy's turret is not this team's to know about or to space from.
    enemy = _own(TURRET, (POST[0] + 5.0, POST[1], 100.0), owner=9, team=TEAM2)
    assert brain._class_deploy_intent(_cooperative(engineer, entities=(enemy,)), engineer,
                                      _state(engineer), 100.0, _defend()) is not None
    scout = _bot(C.CLASS_SCOUT, (RADAR,), ((RADAR, 1),))
    intent = brain._class_deploy_intent(_cooperative(scout), scout, _state(scout), 100.0, _defend())
    assert intent.action.tool_id == RADAR
    live = _own(RADAR, (POST[0] - 40.0, POST[1], 100.0), radius=0.0)
    assert brain._class_deploy_intent(_cooperative(scout, entities=(live,)), scout, _state(scout),
                                      100.0, _defend()) is None


# --- disguise ----------------------------------------------------------------

def test_engineer_disguises_only_while_standing_quietly_at_its_post():
    world, brain = _brain()
    engineer = _bot(C.CLASS_ENGINEER, (DISGUISE,), ((DISGUISE, 2),))
    intent = brain._class_deploy_intent(
        _cooperative(engineer), engineer, _state(engineer), 100.0, _defend())
    assert intent.action.kind is BotActionKind.DEPLOY and intent.action.tool_id == DISGUISE
    assert intent.action.position is None
    # Retail Disguise breaks on any step, so it never starts on one.
    assert intent.movement.direction == (0.0, 0.0, 0.0) and not intent.movement.jump
    walking = replace(engineer, velocity=(2.0, 0.0, 0.0))
    hurt = replace(engineer, last_damage_at=97.0)
    arriving = _at(engineer, (POST[0] - 6.0, POST[1], POST[2]))
    for body in (walking, hurt, arriving):
        assert brain._class_deploy_intent(
            _cooperative(body), body, _state(body), 100.0, _defend()) is None
    state = _state(engineer)
    state.contact_position, state.contact_until = (POST[0] + 30.0, POST[1], POST[2]), 103.0
    assert brain._class_deploy_intent(_cooperative(engineer), engineer, state, 100.0, _defend()) is None


# --- Block Cannon ------------------------------------------------------------

def test_block_cannon_shoots_a_wall_up_from_the_ground_outside_its_own_splash():
    world, brain = _brain()
    engineer = _bot(C.CLASS_ENGINEER, (CANNON,))
    state = _state(engineer)
    decision = _defend()
    built = []
    now = 100.0
    for _ in range(80):
        intent = brain._class_deploy_intent(
            _cooperative(engineer, now=now), engineer, state, now, decision)
        if intent is None:
            break
        assert intent.debug_role == "block_cannon_cover" and intent.tool_id == CANNON
        if intent.action.kind is BotActionKind.ORIENTED:
            aim = intent.action.position
            # The shot lands in the free cell above what it hits.
            cell = (math.floor(aim[0]), math.floor(aim[1]), int(aim[2]) - 1)
            assert world.solid(cell[0], cell[1], cell[2] + 1) and not world.solid(*cell)
            assert math.dist(engineer.eye, aim) > PROJECTILE_SPECS[CANNON].blast_radius + 1.0
            built.append((now, cell))
            world._vxl.set_solid(*cell, True)
        now += 0.125
    assert len(built) == 6
    lower, upper = {cell for _, cell in built[:3]}, {cell for _, cell in built[3:]}
    assert {z for _, _, z in lower} == {99} and {z for _, _, z in upper} == {98}
    assert {(x, y) for x, y, _ in lower} == {(x, y) for x, y, _ in upper}
    # Across the approach: one column of x, three of y.
    assert len({x for x, _, _ in lower}) == 1 and len({y for _, y, _ in lower}) == 3
    # One shot, then time for the block to arrive.
    assert all(later - earlier >= 0.6 for (earlier, _), (later, _) in zip(built, built[1:]))
    # Built: it is not started again straight away.
    assert brain._class_deploy_intent(
        _cooperative(engineer, now=now), engineer, state, now, decision) is None


def test_block_cannon_wall_is_dropped_when_the_body_leaves_the_spot_it_was_measured_from():
    world, brain = _brain()
    engineer = _bot(C.CLASS_ENGINEER, (CANNON,))
    state = _state(engineer)
    first = brain._class_deploy_intent(_cooperative(engineer), engineer, state, 100.0, _defend())
    assert first.action.kind is BotActionKind.ORIENTED and state.cannon_cells
    # Two blocks nearer, the same wall would be inside the cannon's own splash.
    nearer = _at(engineer, (POST[0] + 2.0, POST[1], POST[2]))
    assert brain._class_deploy_intent(
        _cooperative(nearer, now=100.7), nearer, state, 100.7, _defend()) is None
    assert state.cannon_cells == ()


def test_block_cannon_wall_the_server_refuses_is_not_shot_at_again():
    world, brain = _brain()
    engineer = _bot(C.CLASS_ENGINEER, (CANNON,))
    state = _state(engineer)
    first = brain._class_deploy_intent(_cooperative(engineer), engineer, state, 100.0, _defend())
    assert first.action.kind is BotActionKind.ORIENTED
    # A refusal from before this wall was planned says nothing about it.
    stale = replace(engineer, last_action_kind="oriented", last_action_accepted=False,
                    last_action_position=first.action.position, last_action_at=60.0)
    assert brain._class_deploy_intent(
        _cooperative(stale, now=100.25), stale, state, 100.25, _defend()) is not None
    refused = replace(stale, last_action_at=100.1)
    assert brain._class_deploy_intent(
        _cooperative(refused, now=100.7), refused, state, 100.7, _defend()) is None
    assert state.cannon_cells == ()
    # And the next wall is not for a while.
    assert brain._class_deploy_intent(
        _cooperative(engineer, now=110.0), engineer, state, 110.0, _defend()) is None


def test_block_cannon_needs_blocks_level_ground_and_a_clear_site():
    world, brain = _brain()
    poor = _bot(C.CLASS_ENGINEER, (CANNON,), blocks=5)
    assert brain._class_deploy_intent(_cooperative(poor), poor, _state(poor), 100.0, _defend()) is None
    engineer = _bot(C.CLASS_ENGINEER, (CANNON,))
    friend = _player(2, TEAM1, (POST[0] + 6.5, POST[1], POST[2]), is_bot=True)
    blocked = brain._class_deploy_intent(
        _cooperative(engineer, friend), engineer, _state(engineer), 100.0, _defend())
    # Not on a teammate: further out, or not at all.
    assert blocked is None or blocked.action.position[0] - friend.position[0] >= 2.0
    hole_world, hole_brain = _brain()
    hole_world._vxl = SimpleNamespace(
        get_solid=lambda x, y, z: z >= (104 if x >= 25 else 100))
    assert hole_brain._class_deploy_intent(
        _cooperative(engineer), engineer, _state(engineer), 100.0, _defend()) is None


# --- Miner charges -----------------------------------------------------------

def _base_wall():
    """A two-wide, three-high block of enemy base three cells east of the post."""
    return {(x, y, z) for x in (23, 24) for y in (19, 20, 21) for z in (97, 98, 99)}


def _demolish():
    return ModeBotDecision((23.5, 20.5, 97.75), "demolition_assault_base",
                           directive="demolish", posture=ModeBotPosture.ASSAULT,
                           objective_priority=0.9)


@pytest.mark.parametrize("tool, name", [(DYNAMITE, "dynamite"), (C4, "c4")])
def test_miner_sets_its_charge_on_the_demolition_base_facing_itself(tool, name):
    wall = _base_wall()
    world, brain = _brain(wall)
    base = ObjectiveSnapshot("dem_base", TEAM2, (23.5, 20.5, 98.0),
                             bounds=(23, 24, 19, 21, 97, 99), cells=tuple(sorted(wall)))
    miner = _bot(C.CLASS_MINER, (tool,), ((tool, 1),), position=(21.5, 20.5, 97.75))
    state = _state(miner)
    intent = brain._plant_charge_intent(
        _cooperative(miner, objectives=(base,)), miner, state, 100.0, _demolish())
    assert intent.action.kind is BotActionKind.DEPLOY and intent.action.tool_id == tool
    cell = tuple(math.floor(value) for value in intent.action.position)
    assert cell in wall and math.dist(miner.position, intent.action.position) <= 4.5
    assert intent.action.face == 0  # the west face, the one the Miner is looking at
    assert intent.debug_role == f"plant_{name}_objective"
    # It does not plant again while that request is out.
    assert brain._plant_charge_intent(
        _cooperative(miner, objectives=(base,), now=100.5), miner, state, 100.5, _demolish()) is None


def test_no_charge_without_stock_beside_a_teammate_or_while_carrying():
    wall = _base_wall()
    world, brain = _brain(wall)
    base = ObjectiveSnapshot("dem_base", TEAM2, (23.5, 20.5, 98.0),
                             bounds=(23, 24, 19, 21, 97, 99), cells=tuple(sorted(wall)))
    miner = _bot(C.CLASS_MINER, (DYNAMITE,), ((DYNAMITE, 1),), position=(21.5, 20.5, 97.75))
    spent = replace(miner, deployable_stock=((DYNAMITE, 0),))
    carrier = replace(miner, carried_entity_id=3)
    for body in (spent, carrier):
        assert brain._plant_charge_intent(
            _cooperative(body, objectives=(base,)), body, _state(body), 100.0, _demolish()) is None
    friend = _player(2, TEAM1, (19.5, 23.5, 97.75), is_bot=True)
    state = _state(miner)
    assert brain._plant_charge_intent(
        _cooperative(miner, friend, objectives=(base,)), miner, state, 100.0, _demolish()) is None
    assert state.next_charge_at == pytest.approx(102.0)


def test_miner_runs_clear_of_its_own_dynamite_then_waits_for_it():
    world, brain = _brain()
    miner = _bot(C.CLASS_MINER, (DYNAMITE,), position=(21.5, 20.5, 97.75))
    charge = _own(DYNAMITE, (23.5, 20.5, 98.5))
    state = _state(miner)
    frame = _cooperative(miner, entities=(charge,))
    world.begin_planning((1, 1), 100.0)
    fleeing = brain._own_charge_intent(frame, miner, state, 100.0, fighting=True)
    world.end_planning()
    # Even in a firefight: out of the blast first.
    assert fleeing.debug_role.startswith("clear_own_charge")
    assert math.dist(state.charge_retreat, charge.position) >= 5.0 + 2.5
    assert state.charge_retreat[0] < miner.position[0]  # away, on the side it stands
    clear = _at(miner, (14.5, 20.5, 97.75))
    waiting = brain._own_charge_intent(
        _cooperative(clear, entities=(charge,)), clear, state, 101.0, fighting=False)
    assert waiting.debug_role == "await_own_charge"
    assert waiting.movement.direction == (0.0, 0.0, 0.0)
    # A fight outside the blast is fought, and a blown charge is forgotten.
    assert brain._own_charge_intent(
        _cooperative(clear, entities=(charge,)), clear, state, 101.0, fighting=True) is None
    assert brain._own_charge_intent(_cooperative(clear), clear, state, 108.0, fighting=False) is None
    # Somebody else's charge is the grenade-evasion code's business.
    theirs = _own(DYNAMITE, (23.5, 20.5, 98.5), owner=9)
    assert brain._own_charge_intent(
        _cooperative(miner, entities=(theirs,)), miner, _state(miner), 100.0, fighting=False) is None


def test_c4_is_fired_only_from_outside_its_blast_and_never_under_a_teammate():
    world, brain = _brain()
    miner = _bot(C.CLASS_MINER, (C4,), position=(21.5, 20.5, 97.75))
    charge = _own(C4, (23.5, 20.5, 98.5), radius=8.0)
    state = _state(miner)
    world.begin_planning((1, 1), 100.0)
    inside = brain._own_charge_intent(
        _cooperative(miner, entities=(charge,)), miner, state, 100.0, fighting=False)
    world.end_planning()
    assert inside.debug_role.startswith("clear_own_charge")
    assert inside.action.kind is BotActionKind.NONE
    clear = _at(miner, (11.5, 20.5, 97.75))
    fire = brain._own_charge_intent(
        _cooperative(clear, entities=(charge,)), clear, state, 101.0, fighting=False)
    assert fire.debug_role == "detonate_c4" and fire.action.tool_id == C4
    assert fire.action.kind is BotActionKind.DEPLOY and fire.action.argument == "detonate"
    friend = _player(2, TEAM1, (26.5, 22.5, 97.75), is_bot=True)
    held = brain._own_charge_intent(
        _cooperative(clear, friend, entities=(charge,), now=103.0), clear, state, 103.0,
        fighting=False)
    assert held is None  # the charge keeps until he has walked on


def test_defending_miner_lays_c4_down_the_approach_outside_the_blast_of_its_post():
    world, brain = _brain()
    miner = _bot(C.CLASS_MINER, (C4,), ((C4, 2),))
    state = _state(miner)
    walking = brain._plant_charge_intent(_cooperative(miner), miner, state, 100.0, _defend())
    assert walking.debug_role.startswith("lay_c4_trap") and walking.movement.direction[0] > 0.9
    cell = state.trap_cell
    centre = (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)
    # Far enough out that the post itself is clear of the blast.
    assert math.dist(centre, POST) >= float(C.C4_EXPLOSION_RADIUS) + 2.5
    assert world.solid(*cell) and not world.solid(cell[0], cell[1], cell[2] - 1)
    there = _at(miner, state.trap_stand)
    plant = brain._plant_charge_intent(_cooperative(there, now=104.0), there, state, 104.0, _defend())
    assert plant.debug_role == "plant_c4_trap" and plant.action.tool_id == C4
    assert plant.action.position == centre and plant.action.face == 4
    assert state.charge_trap and state.trap_cell is None


def test_no_c4_trap_with_dynamite_under_fire_away_from_the_post_or_too_soon():
    world, brain = _brain()
    decision = _defend()
    dynamite = _bot(C.CLASS_MINER, (DYNAMITE,), ((DYNAMITE, 1),))
    assert brain._plant_charge_intent(
        _cooperative(dynamite), dynamite, _state(dynamite), 100.0, decision) is None
    miner = _bot(C.CLASS_MINER, (C4,), ((C4, 2),))
    hurt = replace(miner, last_damage_at=97.0)
    away = _at(miner, (POST[0] - 9.0, POST[1], POST[2]))
    for body in (hurt, away):
        assert brain._plant_charge_intent(
            _cooperative(body), body, _state(body), 100.0, decision) is None
    attack = ModeBotDecision(POST, "ctf_capture_intel", posture=ModeBotPosture.ASSAULT,
                             objective_priority=0.9)
    assert brain._plant_charge_intent(
        _cooperative(miner), miner, _state(miner), 100.0, attack) is None
    # A wall across the approach hides the spot from the post: no trap there.
    wall = {(x, y, z) for x in (26, 27) for y in range(10, 31) for z in range(94, 100)}
    blind_world, blind_brain = _brain(wall)
    state = _state(miner)
    assert blind_brain._plant_charge_intent(
        _cooperative(miner), miner, state, 100.0, decision) is None
    assert state.next_trap_at == pytest.approx(130.0)


def test_a_c4_trap_is_fired_under_a_visible_enemy_and_only_then():
    world, brain = _brain()
    miner = _bot(C.CLASS_MINER, (C4,), ((C4, 1),))
    charge = _own(C4, (32.5, 20.5, 100.0), radius=8.0)
    state = _state(miner)
    state.charge_trap = True

    def decide(*others, now, fighting=False):
        return brain._own_charge_intent(
            _cooperative(miner, *others, entities=(charge,), now=now), miner, state, now,
            fighting=fighting)

    # Back at its post, outside the blast: it holds the post, not the detonator.
    assert decide(now=110.0) is None
    far = _player(5, TEAM2, (40.5, 20.5, 97.75))
    assert decide(far, now=111.0, fighting=True) is None
    on_it = _player(5, TEAM2, (34.5, 21.5, 97.75))
    friend = _player(2, TEAM1, (30.5, 24.5, 97.75), is_bot=True)
    assert decide(on_it, friend, now=112.0, fighting=True) is None
    fire = decide(on_it, now=113.0, fighting=True)
    assert fire.debug_role == "detonate_c4" and fire.action.argument == "detonate"
    # An enemy it cannot see is not one it knows to be there.
    wall = {(x, y, z) for x in (26, 27) for y in range(10, 31) for z in range(94, 100)}
    blind_world, blind_brain = _brain(wall)
    blind_state = _state(miner)
    blind_state.charge_trap = True
    assert blind_brain._own_charge_intent(
        _cooperative(miner, on_it, entities=(charge,), now=113.0), miner, blind_state, 113.0,
        fighting=False) is None


def test_a_bot_that_came_back_as_another_class_leaves_its_old_c4_alone():
    world, brain = _brain()
    charge = _own(C4, (32.5, 20.5, 100.0), radius=8.0)
    scout = _bot(C.CLASS_SCOUT, (LANDMINE,), ((LANDMINE, 3),))
    # Outside the blast: no detonator to press. Inside it: nothing to run from.
    assert brain._own_charge_intent(
        _cooperative(scout, entities=(charge,)), scout, _state(scout), 100.0, fighting=False) is None
    beside = _at(scout, (30.5, 20.5, 97.75))
    assert brain._own_charge_intent(
        _cooperative(beside, entities=(charge,)), beside, _state(beside), 100.0,
        fighting=False) is None
    # Its old dynamite still burns whoever it is now.
    lit = _own(DYNAMITE, (31.5, 20.5, 98.5))
    world.begin_planning((1, 1), 100.0)
    fleeing = brain._own_charge_intent(
        _cooperative(beside, entities=(lit,)), beside, _state(beside), 100.0, fighting=False)
    world.end_planning()
    assert fleeing.debug_role.startswith("clear_own_charge")


def test_charge_goes_on_a_wall_an_enemy_went_behind_not_on_a_hillside():
    wall = {(24, y, z) for y in range(16, 26) for z in (96, 97, 98, 99)}
    world, brain = _brain(wall)
    miner = _bot(C.CLASS_MINER, (DYNAMITE,), ((DYNAMITE, 1),), position=(21.5, 20.5, 97.75))
    contact = (29.5, 20.5, 97.75)
    cell = brain._cover_cell(miner, contact)
    assert cell is not None and cell[0] == 24
    state = _state(miner)
    state.contact_position, state.contact_until = contact, 103.0
    intent = brain._plant_charge_intent(_cooperative(miner), miner, state, 100.0, None)
    assert intent.debug_role == "plant_dynamite_cover" and intent.action.face == 0
    # In plain view there is nothing to bring down.
    open_world, open_brain = _brain()
    assert open_brain._cover_cell(miner, contact) is None
    # Six blocks of hill is not cover a charge opens.
    hill = {(x, y, z) for x in range(24, 30) for y in range(16, 26) for z in (96, 97, 98, 99)}
    hill_world, hill_brain = _brain(hill)
    assert hill_brain._cover_cell(miner, (33.5, 20.5, 97.75)) is None
    # Out of arm's reach, or an enemy too far to matter.
    far = _at(miner, (17.5, 20.5, 97.75))
    assert brain._cover_cell(far, contact) is None
    assert brain._cover_cell(miner, (44.5, 20.5, 97.75)) is None


def test_a_built_wall_before_an_attacked_objective_is_a_target_and_map_rock_is_not():
    wall = {(24, y, z) for y in range(16, 26) for z in (96, 97, 98, 99)}
    world, brain = _brain(wall)
    miner = _bot(C.CLASS_MINER, (DYNAMITE,), ((DYNAMITE, 1),), position=(21.5, 20.5, 97.75))
    attack = ModeBotDecision((30.5, 20.5, 97.75), "multihill_contest",
                             posture=ModeBotPosture.ASSAULT, objective_priority=0.9)
    assert brain._plant_charge_intent(_cooperative(miner), miner, _state(miner), 100.0, attack) is None
    for cell in wall:
        world._cell_health[cell] = 9.0  # somebody built it
    intent = brain._plant_charge_intent(_cooperative(miner), miner, _state(miner), 100.0, attack)
    assert intent is not None and intent.debug_role == "plant_dynamite_fortification"
    defending = _defend(position=(30.5, 20.5, 97.75))
    assert brain._plant_charge_intent(
        _cooperative(miner), miner, _state(miner), 100.0, defending) is None


# --- the decision flow -------------------------------------------------------

def test_cooperative_decisions_reach_the_class_tools_and_classic_ones_are_unchanged(monkeypatch):
    world, brain = _brain()
    scout = _bot(C.CLASS_SCOUT, (LANDMINE,), ((LANDMINE, 3),))
    monkeypatch.setattr(brain.mode_policy, "decide", lambda *_args, **_kwargs: _defend())
    world.begin_planning((1, 1), 100.0)
    intent = brain.decide(_cooperative(scout))
    world.end_planning()
    assert intent.action.kind is BotActionKind.DEPLOY and intent.action.tool_id == LANDMINE
    # The classic path keeps its own rule: no in-place mine for a 0.8 priority job.
    classic_world, classic_brain = _brain()
    monkeypatch.setattr(classic_brain.mode_policy, "decide", lambda *_args, **_kwargs: _defend())
    classic_world.begin_planning((1, 1), 100.0)
    classic = classic_brain.decide(_frame(scout))
    classic_world.end_planning()
    assert classic.action.kind is not BotActionKind.DEPLOY or classic.action.position == scout.position


def test_own_charge_outranks_a_visible_enemy_in_the_cooperative_decision(monkeypatch):
    world, brain = _brain()
    miner = _bot(C.CLASS_MINER, (DYNAMITE,), position=(21.5, 20.5, 97.75))
    enemy = _player(5, TEAM2, (31.5, 20.5, 97.75))
    charge = _own(DYNAMITE, (23.5, 20.5, 98.5))
    world.begin_planning((1, 1), 100.0)
    intent = brain.decide(_cooperative(miner, enemy, entities=(charge,)))
    world.end_planning()
    assert intent.debug_role.startswith("clear_own_charge")


# --- gateway -----------------------------------------------------------------

class _Services:
    def __init__(self):
        self.calls = []

    def place_dynamite(self, player, position, *, face=4):
        self.calls.append(("dynamite", tuple(position), face))
        return True

    def place_c4(self, player, position, *, face):
        self.calls.append(("c4", tuple(position), face))
        return True

    def detonate_c4(self, player):
        self.calls.append(("detonate",))
        return True


def _gateway_bot(tool):
    player = SimpleNamespace(id=1, team=TEAM1, is_bot=True, alive=True, spawned=True,
                             tool=-1, loadout=(tool,), x=21.5, y=20.5, z=97.75)
    player.set_tool = lambda tool_id, raw=True: setattr(player, "tool", int(tool_id))
    services = _Services()
    server = SimpleNamespace(deployable_actions=services, players={1: player},
                             entity_registry=None, config=None)
    return BotActionGateway(server), player, services


def test_gateway_passes_the_charge_face_and_fires_the_c4_detonator():
    gateway, player, services = _gateway_bot(DYNAMITE)
    spot = (23.5, 20.5, 98.5)
    assert gateway.execute(player, BotAction(BotActionKind.DEPLOY, DYNAMITE, position=spot, face=0))
    assert gateway.execute(player, BotAction(BotActionKind.DEPLOY, DYNAMITE, position=spot))
    assert services.calls == [("dynamite", spot, 0), ("dynamite", spot, 4)]
    gateway, player, services = _gateway_bot(C4)
    assert gateway.execute(player, BotAction(BotActionKind.DEPLOY, C4, argument="detonate"))
    assert services.calls == [("detonate",)] and player.tool == C4
    # Without the argument a C4 action still needs somewhere to go.
    assert not gateway.execute(player, BotAction(BotActionKind.DEPLOY, C4))
    # The detonator belongs to whoever carries C4.
    gateway, player, services = _gateway_bot(DYNAMITE)
    assert not gateway.execute(player, BotAction(BotActionKind.DEPLOY, C4, argument="detonate"))
    assert services.calls == []


def test_gateway_lets_a_block_cannon_shot_land_just_outside_its_splash():
    gateway, player, _services = _gateway_bot(CANNON)
    player.eye = (21.5, 20.5, 96.75)
    hits = []

    def raycast(ex, ey, ez, dx, dy, dz, reach):
        hits.append(reach)
        return (27, 20, 100) if reach >= 7.0 else None

    gateway.server.world_manager = SimpleNamespace(raycast=raycast)
    cannon, rocket = PROJECTILE_SPECS[CANNON], PROJECTILE_SPECS[int(C.RPG_TOOL)]
    # Terrain seven blocks down the barrel: fine for a block, never for a rocket.
    assert gateway._oriented_launch_safe(player, (1.0, 0.0, 0.0), cannon, clearance=1.0)
    assert hits[-1] == pytest.approx(cannon.blast_radius + 1.0)
    assert not gateway._oriented_launch_safe(player, (1.0, 0.0, 0.0), rocket)
    assert hits[-1] == pytest.approx(rocket.blast_radius + 3.0)
    # Unchanged default: the cannon inside the old margin is still refused.
    assert not gateway._oriented_launch_safe(player, (1.0, 0.0, 0.0), cannon)


# --- team layer --------------------------------------------------------------

def test_a_miner_gives_turns_to_prefab_cover_only_when_it_can_pay_for_one():
    world, _ = _brain()
    layer = CooperativeBehavior(world)
    miner = replace(_bot(C.CLASS_MINER, (DYNAMITE,), blocks=12), prefabs=("prefab_superpole",))
    world.prefab_geometry = {"prefab_superpole": SimpleNamespace(block_count=126)}
    # It spawns with no blocks and its cheapest prefab costs 126.
    assert not layer._affords_prefab(miner)
    assert layer._affords_prefab(replace(miner, blocks=126))
    assert not layer._affords_prefab(replace(miner, blocks=500, prefabs=("prefab_unknown",)))
