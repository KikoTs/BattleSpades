"""Cooperative triage, sensory fairness, resource use and task lifetimes."""

from dataclasses import replace
import math
from types import SimpleNamespace

import pytest

import shared.constants as C

from server.bot_ai.behavior_memory import BehaviorMemory
from server.bot_ai.cooperative_behavior import CooperativeBehavior, _Life, identity
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import BotActionKind, EntitySnapshot, PerceptionFrame, Stimulus, StimulusKind
from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain
from server.deployable_inventory import deployable_inventory_snapshot
from tests.test_bot_project_sites import make_world, player
from tests.test_simple_bot_tactics import _profile
from tests.test_equipment_handlers import _server_player


def medic(**kwargs):
    return player(class_id=int(C.CLASS_MEDIC), tool=int(C.LIGHT_MACHINE_GUN_TOOL),
        weapon_tool=int(C.LIGHT_MACHINE_GUN_TOOL),
        loadout=(int(C.LIGHT_MACHINE_GUN_TOOL), int(C.PICKAXE_TOOL), int(C.MEDPACK_TOOL)),
        prefabs=(), deployable_stock=((int(C.MEDPACK_TOOL), 2),), **kwargs)


def frame(observer, *others, now=100., frame_id=1, **kwargs):
    return PerceptionFrame(frame_id, 1, 1, 0, observer.player_id, observer.generation,
        now, "tdm", (observer, *others), profile=_profile(), behavior_version="cooperative", **kwargs)


def test_pair_places_one_pack_even_during_visible_combat_then_patient_uses_it():
    brain = SimpleBotBrain(make_world())
    lead = medic()
    patient = medic(player_id=2, health=40, position=(22.5, 20.5, 17.75), eye=(22.5, 20.5, 17.75))
    enemy = player(player_id=9, team=3, is_bot=False, position=(40.5, 20.5, 17.75), eye=(40.5, 20.5, 17.75))
    first = brain.decide(frame(lead, patient, enemy))
    assert first.action.kind is BotActionKind.DEPLOY
    assert first.action.tool_id == int(C.MEDPACK_TOOL)
    second = brain.decide(frame(patient, lead, enemy, frame_id=2))
    assert second.action.kind is not BotActionKind.DEPLOY
    pack = EntitySnapshot(8, int(C.MEDPACK_ENTITY), lead.team, lead.player_id,
        first.action.position, tool_id=int(C.MEDPACK_TOOL), uses_remaining=3)
    acknowledged = replace(lead, last_action_request_id=first.action.request_id,
                           last_task_accepted=True, deployable_stock=((int(C.MEDPACK_TOOL), 1),))
    brain.decide(frame(acknowledged, patient, now=100.7, frame_id=3, entities=(pack,)))
    use = brain.decide(frame(patient, acknowledged, now=100.8, frame_id=4, entities=(pack,)))
    assert use.debug_role.startswith("use_medpack")
    healed = replace(patient, health=65)
    brain.decide(frame(healed, acknowledged, now=101.1, frame_id=5, entities=(replace(pack, uses_remaining=2),)))
    assert brain.cooperative.teams.metrics["tasks_completed"] >= 1


def test_actual_gateway_pack_heals_and_debits_from_cooperative_action():
    server, owner, _ = _server_player(C.MEDPACK_TOOL, [C.MEDPACK_TOOL, C.LIGHT_MACHINE_GUN_TOOL])
    owner.is_bot = True
    owner.health = 40
    world = SimpleVoxelWorld()
    world._vxl = SimpleNamespace(get_solid=server.world_manager.get_solid,
                                  surface_z=server.world_manager.get_height)
    bot = medic(player_id=owner.id, team=owner.team, health=40,
        position=owner.position, eye=owner.position)
    brain = SimpleBotBrain(world)
    intent = brain.decide(frame(bot))
    assert intent.action.kind is BotActionKind.DEPLOY
    assert BotActionGateway(server).execute(owner, intent.action)
    assert dict(deployable_inventory_snapshot(owner))[int(C.MEDPACK_TOOL)] == 1
    pack = server.entity_registry.all()[0]
    assert pack.behavior.on_touch(pack, owner, server._build_entity_ctx())
    assert owner.health == 65 and pack.behavior.uses == 2
    entity = EntitySnapshot(pack.entity_id, int(C.MEDPACK_ENTITY), owner.team,
        owner.id, (pack.x, pack.y, pack.z), tool_id=int(C.MEDPACK_TOOL), uses_remaining=2)
    updated = replace(bot, health=owner.health, last_action_request_id=intent.action.request_id,
                      last_task_accepted=True, deployable_stock=deployable_inventory_snapshot(owner))
    brain.decide(frame(updated, now=100.5, frame_id=2, entities=(entity,)))
    assert brain.cooperative.teams.metrics["tasks_completed"] == 1
    assert brain.cooperative.teams.events[-1]["reason"] == "patient_healed"


def test_rejected_pack_backs_off_and_never_infers_success_from_old_entity():
    behavior = CooperativeBehavior(make_world())
    bot = medic(health=40)
    first = behavior.decide(frame(bot), bot, None, None)
    assert first.action.kind is BotActionKind.DEPLOY
    rejected = replace(bot, last_action_request_id=first.action.request_id,
                       last_task_accepted=False, last_action_reason="no_stock",
                       deployable_stock=((int(C.MEDPACK_TOOL), 0),))
    result = behavior.decide(frame(rejected, now=100.6, frame_id=2), rejected, None, None)
    assert result is None
    assert behavior.teams.events[-1]["reason"] == "no_stock"
    assert behavior.teams.metrics["actions_confirmed"] == 0
    assert behavior.decide(frame(bot, now=101.5, frame_id=3), bot, None, None) is None


def test_no_stock_and_healthy_medic_pairs_do_not_spawn_infinite_healing():
    behavior = CooperativeBehavior(make_world())
    bot = medic(health=40, player_id=2)
    bot = replace(bot, deployable_stock=((int(C.MEDPACK_TOOL), 0),))
    assert behavior.decide(frame(bot), bot, None, None) is None
    healthy = medic()
    assert behavior.decide(frame(healthy, now=101, frame_id=2), healthy, None, None) is None
    assert behavior.teams.metrics["actions_requested"] == 0


def test_memory_keeps_heard_uncertainty_and_does_not_follow_hidden_player_snapshot():
    behavior = CooperativeBehavior(make_world())
    bot = medic()
    enemy = player(player_id=9, team=3, position=(35.5, 20.5, 17.75))
    heard = Stimulus(StimulusKind.SHOT, (30.5, 20.5, 17.75), 99, 104, team=3, uncertainty=5)
    first = behavior.decide(frame(bot, enemy, stimuli=(heard,)), bot, None, None)
    assert first.goal == heard.position
    moved_hidden = replace(enemy, position=(45.5, 20.5, 17.75))
    later = behavior.decide(frame(bot, moved_hidden, now=101, frame_id=2), bot, None, None)
    assert later.goal == heard.position
    assert behavior.decide(frame(bot, now=105, frame_id=3), bot, None, None) is None


def test_no_mutual_medic_follow_loop_and_incomplete_roster_does_not_forget_other_bots():
    brain = SimpleBotBrain(make_world())
    lead = medic()
    follower = medic(player_id=2, position=(30.5, 20.5, 17.75), eye=(30.5, 20.5, 17.75))
    brain.decide(frame(lead, follower))
    intent = brain.decide(frame(follower, lead, frame_id=2))
    assert intent.debug_role == "medic_partner"
    assert brain.cooperative.lives[(lead.player_id, lead.generation)].partner is None
    original = brain._states[(lead.player_id, lead.generation)]
    brain.decide(frame(follower, now=100.7, frame_id=3))
    assert brain._states[(lead.player_id, lead.generation)] is original


def test_respawn_and_epoch_change_release_patient_claim_and_old_task():
    behavior = CooperativeBehavior(make_world())
    bot = medic(health=40)
    behavior.decide(frame(bot), bot, None, None)
    reborn = replace(bot, life_id=1, health=100)
    behavior.decide(frame(reborn, now=101, frame_id=2), reborn, None, None)
    assert not behavior.patients
    assert behavior.lives[(1, 1)].task is None
    new_map = replace(frame(reborn, now=102, frame_id=3), map_epoch=2)
    behavior.decide(new_map, reborn, None, None)
    assert behavior.epoch == (2, 1) and not behavior.teams.projects


def test_sensory_memory_is_bounded_and_expired():
    memory = BehaviorMemory()
    bot = medic()
    for index in range(100):
        target = player(player_id=index + 10)
        memory.observe(frame(bot, now=100 + index * .01), bot, target)
        memory.record("site", (index, 0, 0), 100, False)
    assert len(memory.contacts) == 24 and len(memory.outcomes) == 16
    memory.observe(frame(bot, now=120), bot, None)
    assert not memory.contacts


def crate(kind, position, entity_id=30):
    return EntitySnapshot(entity_id, int(kind), -1, -1, position, kind="pickup")


def rifleman(**kwargs):
    return player(loadout=(int(C.RIFLE_TOOL), int(C.BLOCK_TOOL)), prefabs=(),
                  deployable_stock=(), **kwargs)


def at(body, position):
    return replace(body, position=position, eye=position)


def committed(position=(45.5, 20.5, 17.75), role="ctf_defend_intel"):
    return ModeBotDecision(position, role, posture=ModeBotPosture.DEFEND, objective_priority=.9)


def test_a_hurt_bot_walks_to_a_known_crate_it_cannot_see_as_far_as_it_is_hurt():
    world = make_world()
    # A wall between the bot and the crate: crates show on the map regardless.
    world._vxl.solids.update((27, y, z) for y in range(5, 36) for z in range(12, 20))
    health = crate(C.HEALTH_CRATE, (34.5, 20.5, 19.75))
    bot = rifleman(health=25)
    order = CooperativeBehavior(world).decide(frame(bot, entities=(health,)), bot, None, None)
    assert order is not None and order.role == "resupply" and order.goal == health.position
    # A quarter of its health left is worth 48 blocks; a scratch is worth 28.
    far = crate(C.HEALTH_CRATE, (48.5, 34.5, 19.75))
    assert math.dist(bot.position, far.position) > 28
    assert CooperativeBehavior(world).decide(
        frame(bot, entities=(far,)), bot, None, None).role == "resupply"
    scratched = rifleman(health=50)
    assert CooperativeBehavior(world).decide(
        frame(scratched, entities=(far,)), scratched, None, None) is None
    assert CooperativeBehavior(world).decide(
        frame(scratched, entities=(health,)), scratched, None, None).role == "resupply"
    fit = rifleman(health=90)
    assert CooperativeBehavior(world).decide(frame(fit, entities=(health,)), fit, None, None) is None


def test_a_bot_with_nothing_left_to_fire_fetches_ammunition_past_an_enemy_in_view():
    ammo = crate(C.AMMO_CRATE, (36.5, 24.5, 19.75))
    enemy = player(player_id=9, team=3, is_bot=False, position=(32.5, 20.5, 17.75),
                   eye=(32.5, 20.5, 17.75))
    dry = rifleman(ammo_clip=0, ammo_reserve=0)
    behavior = CooperativeBehavior(make_world())
    order = behavior.decide(frame(dry, enemy, entities=(ammo,)), dry, enemy, None)
    assert order is not None and order.role == "resupply" and order.goal == ammo.position
    # The enemy closing to point-blank does not turn an empty gun round.
    close = replace(enemy, position=(24.5, 20.5, 17.75), eye=(24.5, 20.5, 17.75))
    again = behavior.decide(frame(dry, close, now=100.4, frame_id=2, entities=(ammo,)),
                            dry, close, None)
    assert again is not None and again.role == "resupply"
    assert "tasks_failed" not in behavior.teams.metrics
    # With rounds in the gun the fight comes first.
    armed = rifleman(health=25)
    health = crate(C.HEALTH_CRATE, (36.5, 24.5, 19.75))
    assert CooperativeBehavior(make_world()).decide(
        frame(armed, enemy, entities=(health,)), armed, enemy, None) is None


def test_a_badly_hurt_bot_leaves_its_mode_job_for_a_crate_and_finishes_the_errand():
    behavior = CooperativeBehavior(make_world())
    health = crate(C.HEALTH_CRATE, (20.5, 33.5, 19.75))  # thirteen blocks off the route
    bot = rifleman(health=25)
    first = behavior.decide(frame(bot, entities=(health,)), bot, None, committed())
    assert first is not None and first.role == "resupply"
    # The objective moving does not take the errand away and hand it back.
    for tick, objective in enumerate(((45.5, 9.5, 17.75), (9.5, 9.5, 17.75), (45.5, 20.5, 17.75))):
        body = at(bot, (20.5, 21.5 + tick, 17.75))
        order = behavior.decide(frame(body, now=100.5 + tick / 2, frame_id=2 + tick,
                                      entities=(health,)), body, None, committed(objective))
        assert order is not None and order.role == "resupply"
    assert behavior.teams.metrics == {"tasks_started": 1}
    healed = at(replace(bot, health=100), health.position)
    assert behavior.decide(frame(healed, now=104., frame_id=9, entities=()), healed, None,
                           committed()) is None
    assert behavior.teams.events[-1]["reason"] == "supplies_restored"


def test_a_block_dug_on_the_way_to_a_health_crate_does_not_end_the_errand():
    behavior = CooperativeBehavior(make_world())
    health = crate(C.HEALTH_CRATE, (34.5, 20.5, 19.75))
    bot = rifleman(health=25)
    assert behavior.decide(frame(bot, entities=(health,)), bot, None, None).role == "resupply"
    digging = replace(at(bot, (22.5, 20.5, 17.75)), blocks=bot.blocks + 1)
    order = behavior.decide(frame(digging, now=100.5, frame_id=2, entities=(health,)),
                            digging, None, None)
    assert order is not None and order.role == "resupply"
    assert "tasks_completed" not in behavior.teams.metrics
    # Health, from the crate or from anything else, is what it came for.
    better = replace(digging, health=60)
    assert behavior.decide(frame(better, now=101., frame_id=3, entities=(health,)),
                           better, None, None) is None
    assert behavior.teams.events[-1]["reason"] == "supplies_restored"
    # A crate taken from under it still counts any gain, as before.
    behavior = CooperativeBehavior(make_world())
    assert behavior.decide(frame(bot, entities=(health,)), bot, None, None).role == "resupply"
    behavior.decide(frame(digging, now=100.5, frame_id=2, entities=()), digging, None, None)
    assert behavior.teams.events[-1]["reason"] == "supplies_restored"


def test_an_errand_keeps_the_body_through_a_hop_but_not_into_a_fight_or_water():
    health = crate(C.HEALTH_CRATE, (20.5, 33.5, 19.75))
    bot = rifleman(health=25)
    enemy = player(player_id=9, team=3, is_bot=False, position=(32.5, 20.5, 17.75),
                   eye=(32.5, 20.5, 17.75))

    def walking():
        behavior = CooperativeBehavior(make_world())
        first = behavior.decide(frame(bot, entities=(health,)), bot, None, committed())
        assert first.role == "resupply"
        return behavior, first

    def hop(behavior, body, visible=None, now=100.25):
        players = (body,) if visible is None else (body, visible)
        return behavior.decide(frame(*players, now=now, frame_id=2, entities=(health,)),
                               body, visible, committed())

    behavior, first = walking()
    airborne = replace(at(bot, (20.5, 21.5, 17.0)), grounded=False)
    assert hop(behavior, airborne) == first
    # Landing takes the errand up again; nothing was restarted meanwhile.
    landed = at(bot, (20.5, 22.5, 17.75))
    assert behavior.decide(frame(landed, now=100.5, frame_id=3, entities=(health,)),
                           landed, None, committed()).role == "resupply"
    assert behavior.teams.metrics == {"tasks_started": 1}
    # An enemy coming into view mid-hop, water, and a lapsed errand all hand
    # the body back as before.
    behavior, _first = walking()
    assert hop(behavior, airborne, enemy) is None
    behavior, _first = walking()
    assert hop(behavior, replace(airborne, grounded=True, wade=True)) is None
    behavior, _first = walking()
    assert hop(behavior, airborne, now=400.0) is None


@pytest.mark.parametrize("change", ["scratched", "carrier", "escaping", "too_far"])
def test_a_mode_job_is_left_only_by_a_bot_that_is_badly_off_and_free_to_go(change):
    health = crate(C.HEALTH_CRATE, (20.5, 33.5, 19.75))
    bot = rifleman(health=25)
    strategic = committed()
    if change == "scratched":
        bot = replace(bot, health=45)
    elif change == "carrier":
        bot = replace(bot, carried_entity_id=7)
    elif change == "escaping":
        strategic = committed(role="vip_retreat")
    else:
        health = crate(C.HEALTH_CRATE, (49.5, 34.5, 19.75))
    behavior = CooperativeBehavior(make_world())
    assert behavior.decide(frame(bot, entities=(health,)), bot, None, strategic) is None
    assert "tasks_started" not in behavior.teams.metrics


def test_a_crate_that_cannot_be_reached_is_given_up_and_not_tried_again_at_once():
    behavior = CooperativeBehavior(make_world())
    health = crate(C.HEALTH_CRATE, (34.5, 20.5, 19.75))
    bot = rifleman(health=25)
    assert behavior.decide(frame(bot, entities=(health,)), bot, None, None).role == "resupply"
    last = None
    for tick in range(1, 16):
        last = behavior.decide(frame(bot, now=100 + tick / 2, frame_id=1 + tick,
                                     entities=(health,)), bot, None, None)
    assert last is None
    assert behavior.teams.events[-1]["reason"] == "supply_unreachable"
    assert behavior.teams.metrics["tasks_started"] == 1


def test_a_hurt_bot_walks_to_a_teammate_medic_and_uses_the_pack_it_is_given():
    behavior = CooperativeBehavior(make_world())
    patient = rifleman(health=30)
    doctor = medic(player_id=2, position=(46.5, 30.5, 17.75), eye=(46.5, 30.5, 17.75))
    assert math.dist(patient.position, doctor.position) > 24  # beyond where the medic looks
    order = behavior.decide(frame(patient, doctor), patient, None, None)
    assert order is not None and order.role == "seek_medic" and order.goal == doctor.position
    # The medic moves; the patient follows the medic, not the place it was.
    moved = at(doctor, (40.5, 30.5, 17.75))
    order = behavior.decide(frame(patient, moved, now=100.5, frame_id=2), patient, None, None)
    assert order.role == "seek_medic" and order.goal == moved.position
    beside = at(patient, (38.5, 29.5, 17.75))
    waiting = behavior.decide(frame(beside, moved, now=104., frame_id=3), beside, None, None)
    assert waiting.role == "await_medic" and waiting.hold
    pack = EntitySnapshot(8, int(C.MEDPACK_ENTITY), patient.team, 2, (39.5, 29.5, 19.75),
                          tool_id=int(C.MEDPACK_TOOL), uses_remaining=3)
    use = behavior.decide(frame(beside, moved, now=104.5, frame_id=4, entities=(pack,)),
                          beside, None, None)
    assert use.role == "use_medpack" and use.goal == pack.position
    healed = replace(beside, health=55)
    assert behavior.decide(frame(healed, moved, now=105., frame_id=5, entities=(pack,)),
                           healed, None, None) is None
    assert behavior.teams.events[-1]["reason"] == "patient_healed"
    assert behavior.teams.metrics["tasks_started"] == 1


def test_the_nearer_of_medic_and_health_crate_is_chosen_and_an_empty_medic_is_no_use():
    patient = rifleman(health=30)
    doctor = medic(player_id=2, position=(32.5, 20.5, 17.75), eye=(32.5, 20.5, 17.75))

    def choose(*entities):
        behavior = CooperativeBehavior(make_world())
        life = _Life(identity(patient), patient.loadout)
        return behavior._recovery_task(frame(patient, doctor, entities=entities), patient, life,
                                       (patient, doctor)).kind

    assert choose(crate(C.HEALTH_CRATE, (26.5, 20.5, 19.75))) == "resupply"
    assert choose(crate(C.HEALTH_CRATE, (44.5, 20.5, 19.75))) == "seek_medic"
    assert choose() == "seek_medic"
    spent = replace(doctor, deployable_stock=((int(C.MEDPACK_TOOL), 0),))
    assert CooperativeBehavior(make_world()).decide(frame(patient, spent), patient, None, None) is None


def test_a_medic_with_a_mode_job_still_treats_the_teammate_beside_it():
    behavior = CooperativeBehavior(make_world())
    doctor = medic()
    wounded = rifleman(player_id=2, health=40, position=(29.5, 20.5, 17.75),
                       eye=(29.5, 20.5, 17.75))
    order = behavior.decide(frame(doctor, wounded), doctor, None, committed())
    assert order is not None and order.role == "medic_approach"
    assert order.goal == wounded.position
    # Not across the map, and not while carrying the objective.
    distant = at(wounded, (44.5, 30.5, 17.75))
    assert CooperativeBehavior(make_world()).decide(
        frame(doctor, distant), doctor, None, committed()) is None
    carrier = replace(doctor, carried_entity_id=5)
    assert CooperativeBehavior(make_world()).decide(
        frame(carrier, wounded), carrier, None, committed()) is None
