"""Human attention never steals combat, traversal, tools or safe footing."""
from dataclasses import replace

import pytest

from server.bot_ai.human_behavior import HumanBehavior
from server.bot_ai.messages import (
    BotAction, BotActionKind, BotIntent, BotIntentPriority, EntitySnapshot,
    LookIntent, MovementAffordance, MovementIntent, ObjectiveSnapshot,
    Stimulus, StimulusKind,
)
from server.bot_ai.stimuli import BotStimulusBus
from tests.test_bot_combat_tactics import _GridWorld, _fighter
from tests.test_simple_bot_tactics import _frame
import random
import time
from types import SimpleNamespace


def scene(*, actor_id=1, now=100., crouching=False, stimuli=(), **actor_fields):
    actor = replace(_fighter(actor_id, 2, 10.5, 10.5), is_bot=True, **actor_fields)
    human = replace(_fighter(20, 2, 13.5, 10.5), is_bot=False, crouching=crouching,
                    orientation=(-1., 0., 0.))
    frame = replace(_frame(actor, human, created_at=now), stimuli=tuple(stimuli))
    intent = BotIntent(actor.player_id, actor.generation, frame.frame_id, frame.map_epoch,
        frame.mode_epoch, 0, now, now + .4, MovementIntent(),
        tool_id=actor.weapon_tool, debug_role="hold_no_objective:arrived", life_id=actor.life_id)
    return actor, human, frame, intent


def attention(actor, human, *, at=100., life_id=None):
    return Stimulus(StimulusKind.FRIENDLY_ATTENTION, human.position, at, at + 1.5,
        source_id=human.player_id, team=human.team, target_id=actor.player_id,
        target_life_id=actor.life_id if life_id is None else life_id)


def test_courtesy_acknowledgement_retains_yield_and_is_bounded_by_real_danger():
    replies = 0
    for actor_id in range(1, 17):
        behavior = HumanBehavior(_GridWorld())
        actor, human, frame, intent = scene(actor_id=actor_id)
        yielding = replace(intent, social_cue="yield_human",
            movement=MovementIntent(direction=(0., 1., 0.), sneak=True))
        result = behavior.overlay(frame, yielding)
        if result.social_cue != "yield_ack":
            continue
        replies += 1
        assert result.look.target == human.eye and result.look.glance
        assert result.movement == yielding.movement and result.action == yielding.action
        interrupted = replace(frame, created_at=100.1,
            players=(replace(actor, last_damage_at=100.05), human))
        assert behavior.overlay(interrupted, yielding) == yielding
        assert behavior.overlay(replace(frame, created_at=101.), yielding).social_cue != "yield_ack"
    assert 0 < replies < 16


def test_friendly_hit_is_a_brief_delayed_look_and_never_a_retaliation():
    behavior = HumanBehavior(_GridWorld())
    actor, human, frame, intent = scene()
    frame = replace(frame, stimuli=(attention(actor, human),))
    first = behavior.overlay(frame, intent)
    assert not first.social_cue
    reply = behavior.overlay(replace(frame, created_at=100.31), intent)
    assert reply.social_cue == "friendly_hit_ack"
    assert reply.look.target == human.eye and reply.look.glance
    assert reply.action.kind is BotActionKind.NONE and reply.movement == intent.movement
    done = behavior.overlay(replace(frame, created_at=101.), intent)
    assert not done.social_cue


@pytest.mark.parametrize("urgent", ("fire", "reload", "damage", "jump", "swim", "drop", "hazard", "enemy", "awareness"))
def test_threat_or_precise_work_immediately_cancels_a_pending_greeting(urgent):
    behavior = HumanBehavior(_GridWorld())
    actor, human, frame, intent = scene()
    behavior.overlay(replace(frame, stimuli=(attention(actor, human),)), intent)
    frame = replace(frame, created_at=100.31)
    if urgent in {"fire", "reload"}:
        intent = replace(intent, action=BotAction(BotActionKind(urgent)))
    elif urgent == "damage":
        frame = replace(frame, players=(replace(actor, last_damage_at=100.2), human))
    elif urgent in {"jump", "swim", "drop"}:
        intent = replace(intent, movement=MovementIntent(affordance=MovementAffordance(urgent)))
    elif urgent == "hazard":
        frame = replace(frame, entities=(EntitySnapshot(1, 1, 3, -1, actor.position, hazardous=True),))
    elif urgent == "enemy":
        frame = replace(frame, players=frame.players + (_fighter(3, 3, 15., 10.5),))
    else:
        intent = replace(intent, look=LookIntent((15., 10., 20.), glance=True))
    assert behavior.overlay(frame, intent) == intent
    assert behavior.lives[(actor.player_id, actor.generation)].gesture is None


def test_greeting_retains_world_space_travel_without_posture_changes():
    behavior = HumanBehavior(_GridWorld())
    actor, human, frame, intent = scene()
    movement = MovementIntent((1., 0., 0.), sprint=True,
                              travel_source=actor.position, travel_waypoint=(40., 10.5, actor.position[2]))
    intent = replace(intent, movement=movement)
    behavior.overlay(replace(frame, stimuli=(attention(actor, human),)), intent)
    reply = behavior.overlay(replace(frame, created_at=100.31), intent)
    assert reply.social_cue == "friendly_hit_ack"
    assert reply.movement == movement


def test_humans_can_receive_crouch_replies_without_a_synchronized_squad_ritual():
    behavior = HumanBehavior(_GridWorld())
    replies = []
    for bot_id in range(1, 25):
        _, _, frame, intent = scene(actor_id=bot_id, crouching=True)
        behavior.overlay(frame, intent)
        reply = behavior.overlay(replace(frame, created_at=100.31), intent)
        if reply.social_cue:
            replies.append(reply)
    assert len(replies) == 1
    assert replies[0].social_cue == "crouch_greeting"
    assert replies[0].movement.crouch


def test_hold_crouch_and_repeated_taps_do_not_renew_the_gesture():
    behavior = HumanBehavior(_GridWorld())
    actor, human, frame, intent = scene()
    first = attention(actor, human)
    behavior.overlay(replace(frame, stimuli=(first,)), intent)
    for now in (101., 102., 105., 110.):
        tap = attention(actor, human, at=now)
        assert behavior.overlay(replace(frame, created_at=now, stimuli=(tap,)), intent).social_cue not in {
            "friendly_hit_ack", "crouch_greeting"}
    assert behavior.lives[(actor.player_id, actor.generation)].partners[(human.player_id, human.generation)].encounters == 1


@pytest.mark.parametrize("change", ("wall", "behind", "old_life", "team", "disabled"))
def test_social_information_is_local_current_and_optional(change):
    world = _GridWorld()
    behavior = HumanBehavior(world)
    actor, human, frame, intent = scene()
    cue = attention(actor, human, life_id=actor.life_id + 1 if change == "old_life" else None)
    frame = replace(frame, stimuli=(cue,))
    if change == "wall":
        world.has_line_of_sight = lambda *_args: False
    elif change == "behind":
        frame = replace(frame, players=(replace(actor, orientation=(-1., 0., 0.)), human))
    elif change == "team":
        frame = replace(frame, players=(actor, replace(human, team=3)))
    elif change == "disabled":
        frame = replace(frame, social_interactions=False)
    behavior.overlay(frame, intent)
    assert behavior.overlay(replace(frame, created_at=100.31), intent) == intent


def test_infected_assault_greetings_cannot_change_posture_or_stop_movement():
    behavior = HumanBehavior(_GridWorld())
    actor, human, frame, intent = scene(crouching=True)
    frame = replace(frame, mode_id="zom", objectives=(ObjectiveSnapshot(
        "zombie_order", actor.team, (30., 10., 20.), carrier_id=actor.player_id),))
    behavior.overlay(replace(frame, stimuli=(attention(actor, human),)), intent)
    reply = behavior.overlay(replace(frame, created_at=100.31), intent)
    assert reply.movement == intent.movement


def test_completion_is_one_confirmed_event_and_does_not_restart_a_route():
    behavior = HumanBehavior(_GridWorld())
    actor, _, frame, intent = scene()
    behavior.overlay(frame, intent)
    point = (15., 10.5, actor.position[2])
    behavior.completed((actor.player_id, actor.generation, actor.life_id), "bridge", point, 100.1)
    reply = behavior.overlay(replace(frame, created_at=100.2), intent)
    assert reply.social_cue == "completed_bridge"
    assert reply.look.target == point
    assert reply.action == intent.action and reply.movement == intent.movement
    assert not behavior.overlay(replace(frame, created_at=101.), intent).social_cue


def test_multiple_friendly_humans_cannot_chain_attention_to_keep_one_bot_distracted():
    actor, first, frame, intent = scene()
    second = replace(first, player_id=first.player_id+1, position=(14.5,10.5,first.position[2]),
                     eye=(14.5,10.5,first.eye[2]))
    behavior = HumanBehavior(_GridWorld())
    frame = replace(frame, players=(actor, first, second), stimuli=(attention(actor,first),))
    behavior.overlay(frame, intent)
    assert behavior.overlay(replace(frame,created_at=100.31),intent).social_cue == "friendly_hit_ack"
    switched = replace(frame,created_at=101.,stimuli=(attention(actor,second,at=100.9),))
    assert behavior.overlay(switched,intent).social_cue != "friendly_hit_ack"
    ready = replace(frame,created_at=111.,stimuli=(attention(actor,second,at=111.),))
    behavior.overlay(ready,intent)
    assert behavior.overlay(replace(ready,created_at=111.31),intent).social_cue == "friendly_hit_ack"


def test_idle_attention_has_dwell_and_varies_across_the_roster():
    behavior = HumanBehavior(_GridWorld())
    targets = set()
    for bot_id in range(1, 17):
        actor, _, frame, intent = scene(actor_id=bot_id)
        frame = replace(frame, players=(actor,))
        behavior.overlay(frame, intent)
        reply = behavior.overlay(replace(frame, created_at=106.), intent)
        assert reply.social_cue == "idle_look"
        targets.add(reply.look.target)
        again = behavior.overlay(replace(frame, created_at=106.1), intent)
        assert again.look == reply.look
    assert len(targets) == 16


@pytest.mark.parametrize("occluded", (False, True))
def test_shared_attention_requires_the_bot_to_see_the_fresh_event(occluded):
    actor, human, frame, intent = scene()
    human = replace(human, orientation=(1., 0., 0.))
    point = (18.5, 10.5, actor.eye[2])
    cue = Stimulus(StimulusKind.SHOT, point, 100., 102., team=actor.team)
    world = _GridWorld([(16, 10, int(actor.eye[2]))] if occluded else ())
    behavior = HumanBehavior(world)
    frame = replace(frame, players=(actor, human), stimuli=(cue,))
    behavior.overlay(frame, intent)
    result = behavior.overlay(replace(frame, created_at=100.31), intent)
    assert (result.social_cue == "follow_ally_attention") is not occluded
    if not occluded:
        assert result.look.target == point
        assert result.movement == intent.movement
    expired = replace(frame, created_at=103.)
    assert behavior.overlay(expired, intent).social_cue != "follow_ally_attention"


def test_objective_acknowledgement_requires_a_completed_friendly_event_and_has_cooldown():
    actor, _, frame, intent = scene()
    cue = Stimulus(StimulusKind.OBJECTIVE, (15., 10.5, actor.eye[2]), 100., 102., team=actor.team)
    behavior = HumanBehavior(_GridWorld())
    frame = replace(frame, players=(actor,), stimuli=(cue,))
    behavior.overlay(frame, intent)
    result = behavior.overlay(replace(frame, created_at=100.31), intent)
    assert result.social_cue == "objective_ack" and result.action == intent.action
    later = replace(frame, created_at=101., stimuli=(replace(cue, created_at=101.),))
    assert behavior.overlay(later, intent).social_cue != "objective_ack"
    enemy = HumanBehavior(_GridWorld())
    result = enemy.overlay(replace(frame, stimuli=(replace(cue, team=3),)), intent)
    assert result.social_cue != "objective_ack"


def test_friendly_attention_bus_is_delivered_only_to_the_tapped_teammate():
    bus = BotStimulusBus()
    bus.publish(StimulusKind.FRIENDLY_ATTENTION, (0., 0., 0.), source_id=20,
                team=2, target_id=1, target_life_id=8, now=100., radius=8)
    perceive = lambda pid, team: bus.perceive((1., 0., 0.), now=100.1,
        rng=random.Random(1), observer_id=pid, team=team)
    assert len(perceive(1, 2)) == 1
    assert perceive(1, 2)[0].target_life_id == 8
    assert perceive(2, 2) == perceive(1, 3) == ()


@pytest.mark.parametrize("obstacle", ("none", "wall", "away", "out_of_reach"))
def test_native_friendly_melee_attention_needs_real_reach_and_clear_line(obstacle):
    from tests.test_bot_architecture import _facing_fixture
    server, _, bot, _ = _facing_fixture()
    distance = 5. if obstacle == "out_of_reach" else 2.
    origin = (bot.eye[0] - distance, bot.eye[1], bot.eye[2])
    attacker = SimpleNamespace(id=253, team=bot.team, is_bot=False,
        position=origin, eye=origin, orientation=(1., 0., 0.))
    direction = (-1., 0., 0.) if obstacle == "away" else (1., 0., 0.)
    if obstacle == "wall":
        server.world_manager.set_block(int(origin[0] + 1), int(origin[1]), int(origin[2]), True, 0x777777)
    health, position = bot.health, bot.position
    server.combat._resolve_melee_hit(attacker, origin, direction)
    cues = server.bot_stimuli.perceive(bot.position, observer_id=bot.id,
        team=bot.team, now=time.monotonic(), rng=random.Random(1))
    taps = [cue for cue in cues if cue.kind is StimulusKind.FRIENDLY_ATTENTION]
    assert bool(taps) is (obstacle == "none")
    assert bot.health == health and bot.position == position
