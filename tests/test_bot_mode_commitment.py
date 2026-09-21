"""Winning objectives retain ownership across optional cooperative work."""

from dataclasses import replace

import pytest

from server.bot_ai.cooperative_behavior import CooperativeBehavior
from server.bot_ai.messages import BotActionKind, EntitySnapshot, ObjectiveSnapshot
from server.bot_ai.policies import ModeBotDecision, ModePolicyMemory, objective_decision_for
from tests.test_bot_cooperative_continuity import formation
from tests.test_bot_cooperative_medical import frame, medic
from tests.test_bot_policies import _frame, _player
from tests.test_bot_project_sites import make_world, player
import shared.constants as C


def test_small_vip_team_has_both_an_escort_and_an_attacker_without_id_luck():
    first = _player(2, 2, (40., 40., 10.))
    second = _player(4, 2, (42., 40., 10.))
    own = ObjectiveSnapshot("vip", 2, (35., 35., 10.), carrier_id=8)
    enemy = ObjectiveSnapshot("vip", 3, (200., 200., 10.), carrier_id=9)
    observation = _frame("vip", first, second, objectives=(own, enemy))
    roles = {objective_decision_for(observation, bot).role for bot in (first, second)}
    assert roles == {"vip_guard_formation", "vip_flank_attack"}


@pytest.mark.parametrize("role,priority", [
    ("ctf_attack_intel", .82),
    ("ctf_escort", .88),
    ("classic_ctf_defend", .74),
    ("vip_flank_attack", .84),
    ("territory_defend", .86),
    ("demolition_defend_base", .86),
    ("diamond_guard_dropoff", .82),
])
def test_real_mode_goal_cannot_be_replaced_by_optional_human_formation(role, priority):
    coordinator, bot, human = formation()
    strategic = ModeBotDecision((45.5, 20.5, 17.75), role,
                                objective_priority=priority)
    observation = frame(bot, human, friendly_mischief=False)
    assert coordinator.decide(observation, bot, None, strategic) is None
    assert coordinator.lives[(bot.player_id, bot.generation)].partner is None


def test_new_objective_immediately_releases_an_existing_optional_formation():
    coordinator, bot, human = formation()
    observation = frame(bot, human, friendly_mischief=False)
    assert coordinator.decide(observation, bot, None, None) is not None
    strategic = ModeBotDecision((45.5, 20.5, 17.75), "ctf_intercept_carrier",
                                objective_priority=.94)
    assert coordinator.decide(replace(observation, created_at=100.2), bot,
                              None, strategic) is None
    assert coordinator.lives[(bot.player_id, bot.generation)].partner is None


def vip_frame(now=100.):
    guard = _player(1, 3, (40., 40., 10.))
    attacker = _player(3, 3, (230., 40., 10.))
    vip = _player(5, 3, (35., 40., 10.))
    objectives = (
        ObjectiveSnapshot("vip", 3, vip.position, carrier_id=5),
        ObjectiveSnapshot("vip", 2, (250., 40., 10.), carrier_id=6),
        ObjectiveSnapshot("team_anchor", 3, (20., 40., 10.)),
        ObjectiveSnapshot("team_anchor", 2, (260., 40., 10.)),
    )
    return replace(_frame("vip", guard, attacker, vip, objectives=objectives),
                   created_at=now), guard, attacker, vip


def test_enemy_vip_death_preserves_guard_but_attacker_mops_up_remaining_team():
    memory = ModePolicyMemory()
    observation, guard, attacker, _ = vip_frame()
    assert memory.decide(observation, attacker).role == "vip_flank_attack"
    assert memory.decide(observation, guard).role == "vip_guard_formation"
    after = replace(observation, created_at=100.2,
                    objectives=tuple(o for o in observation.objectives
                                     if not (o.kind == "vip" and o.team == 2)))
    attack = memory.decide(after, attacker)
    escort = memory.decide(after, guard)
    assert attack.role == "vip_mop_up" and attack.position == (260., 40., 10.)
    assert escort.role == "vip_guard_formation" and escort.position == (35., 40., 10.)


def test_escort_anchor_ignores_small_vip_motion_but_tracks_meaningful_relocation():
    memory = ModePolicyMemory()
    observation, guard, _, _ = vip_frame()
    first = memory.decide(observation, guard)
    assert first.watch_position == (260., 40., 10.)
    for index, offset in enumerate((.2, -.7, .9, 2.0)):
        moved = replace(observation, created_at=100.2 + index * .2,
                        objectives=tuple(replace(o, position=(35. + offset, 40., 10.))
                                         if o.kind == "vip" and o.team == 3 else o
                                         for o in observation.objectives))
        assert memory.decide(moved, guard).position == first.position
    moved = replace(moved, created_at=101.2,
                    objectives=tuple(replace(o, position=(42., 40., 10.))
                                     if o.kind == "vip" and o.team == 3 else o
                                     for o in observation.objectives))
    assert memory.decide(moved, guard).position == (42., 40., 10.)


def test_vip_self_rallies_to_known_friend_and_retreats_from_received_hit_only():
    memory = ModePolicyMemory()
    observation, guard, _, vip = vip_frame()
    # A friendly at the rear is legal knowledge; a hidden enemy roster row
    # must not change either rally or the direction the VIP watches.
    guard = replace(guard, position=(30., 40., 10.))
    hidden = _player(12, 2, (34., 40., 10.))
    observation = replace(observation, players=(vip, guard, hidden))
    rally = memory.decide(observation, vip)
    assert rally.role == "vip_rally" and rally.position == guard.position
    assert rally.watch_position == (260., 40., 10.)
    hurt = replace(vip, last_damage_at=100.1, last_damage_source_position=(45., 40., 10.))
    retreat = memory.decide(replace(observation, created_at=100.2), hurt)
    assert retreat.role == "vip_retreat" and retreat.position[0] < vip.position[0]
    assert retreat.sprint


@pytest.mark.parametrize("edge", ["map", "phase", "life", "team", "class"])
def test_policy_hysteresis_cannot_survive_authoritative_boundaries(edge):
    memory = ModePolicyMemory()
    observation, guard, _, _ = vip_frame()
    first = memory.decide(observation, guard)
    moved = replace(observation, created_at=100.2,
                    objectives=tuple(replace(o, position=(36., 40., 10.))
                                     if o.kind == "vip" and o.team == 3 else o
                                     for o in observation.objectives))
    if edge == "map":
        moved = replace(moved, map_epoch=2)
    elif edge == "phase":
        moved = replace(moved, mode_phase="intermission")
    elif edge == "life":
        guard = replace(guard, life_id=2)
    elif edge == "class":
        guard = replace(guard, class_id=4)
    else:
        guard = replace(guard, team=2)
    updated = memory.decide(moved, guard)
    assert updated.position != first.position


def test_vip_role_is_retained_across_brief_roster_churn_then_rebalanced():
    memory = ModePolicyMemory()
    observation, _, attacker, _ = vip_frame()
    assert memory.decide(observation, attacker).role == "vip_flank_attack"
    # Add four bot teammates: roster balancing would now make id3 a guard.
    additions = tuple(_player(i, 3, (25., 40., 10.)) for i in (7, 8, 9, 10))
    changed = replace(observation, created_at=101., players=observation.players + additions)
    assert memory.decide(changed, attacker).role == "vip_flank_attack"
    assert memory.decide(replace(changed, created_at=109.), attacker).role == "vip_guard_formation"


def test_urgent_self_healing_borrows_only_bounded_time_from_capture_route():
    coordinator = CooperativeBehavior(make_world())
    bot = medic(health=40, carried_entity_id=99)
    strategic = ModeBotDecision((45.5, 20.5, 17.75), "ctf_capture",
                                objective_priority=.98)
    first = coordinator.decide(frame(bot), bot, None, strategic)
    assert first is not None and first.action.kind is BotActionKind.DEPLOY
    assert first.action.tool_id == int(C.MEDPACK_TOOL)
    task = coordinator.lives[(bot.player_id, bot.generation)].task
    assert task.expires_at == 104.
    # No authoritative heal arrived: give back the winning route, never wait
    # indefinitely or repeatedly renew a medical claim at the same deadline.
    assert coordinator.decide(frame(bot, now=104.1), bot, None, strategic) is None
    assert coordinator.lives[(bot.player_id, bot.generation)].task is None
    assert not coordinator.patients


@pytest.mark.parametrize("pack_position,allowed", [
    ((24.5, 20.5, 17.75), True),
    ((20.5, 26.5, 17.75), False),
    ((34.5, 20.5, 17.75), False),
])
def test_objective_heal_stop_must_be_both_nearby_and_on_the_route(pack_position, allowed):
    coordinator = CooperativeBehavior(make_world())
    bot = player(health=40, loadout=(int(C.RIFLE_TOOL),), prefabs=(), deployable_stock=())
    pack = EntitySnapshot(12, int(C.MEDPACK_ENTITY), bot.team, 8, pack_position,
                          tool_id=int(C.MEDPACK_TOOL), uses_remaining=2)
    strategic = ModeBotDecision((45.5, 20.5, 17.75), "vip_mop_up",
                                objective_priority=.72)
    order = coordinator.decide(frame(bot, entities=(pack,)), bot, None, strategic)
    assert (order is not None) is allowed
    if not allowed:
        assert not coordinator.patients


@pytest.mark.parametrize("role", ["demolition_escape_airstrike", "occupation_dispose_bomb",
                                  "zombie_last_survivor_escape", "vip_retreat"])
def test_immediate_escape_cannot_pause_to_heal_even_in_place(role):
    coordinator = CooperativeBehavior(make_world())
    bot = medic(health=30)
    strategic = ModeBotDecision((45.5, 20.5, 17.75), role, objective_priority=1.)
    assert coordinator.decide(frame(bot), bot, None, strategic) is None
    assert not coordinator.patients
