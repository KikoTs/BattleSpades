"""Cooperative triage, sensory fairness, resource use and task lifetimes."""

from dataclasses import replace
from types import SimpleNamespace

import shared.constants as C

from server.bot_ai.behavior_memory import BehaviorMemory
from server.bot_ai.cooperative_behavior import CooperativeBehavior
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import BotActionKind, EntitySnapshot, PerceptionFrame, Stimulus, StimulusKind
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
