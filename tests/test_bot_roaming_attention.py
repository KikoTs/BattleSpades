"""Roaming attention changes the view, with bounded distraction and safe travel."""
from collections import Counter
from dataclasses import replace
import math

import pytest

from server.bot_ai.human_behavior import HumanBehavior
from server.bot_ai.messages import (
    BotAction, BotActionKind, BotIntentPriority, EntitySnapshot, LookIntent,
    MovementAffordance, MovementIntent, ObjectiveSnapshot,
)
from tests.test_bot_combat_tactics import _GridWorld, _fighter
from tests.test_bot_social_behavior import scene


def roaming_scene(actor_id=1, now=100.):
    actor, _, frame, intent = scene(actor_id=actor_id, now=now)
    movement = MovementIntent(direction=(1., 0., 0.), travel_source=actor.position,
        travel_waypoint=(actor.position[0] + 20., actor.position[1], actor.position[2]),
        gaze_waypoint=(actor.position[0] + 30., actor.position[1], actor.eye[2]))
    return actor, replace(frame, players=(actor,)), replace(intent,
        movement=movement, look=LookIntent(movement.gaze_waypoint),
        priority=BotIntentPriority.TRAVERSAL, debug_role="patrol")


def active_glance(world=None):
    behavior = HumanBehavior(world or _GridWorld())
    actor, frame, intent = roaming_scene()
    behavior.overlay(frame, intent)
    due = behavior.lives[(actor.player_id, actor.generation)].next_roam_at
    frame = replace(frame, created_at=due)
    reply = behavior.overlay(frame, intent)
    assert reply.social_cue == "roam_check"
    return behavior, actor, frame, intent, reply


def test_full_roster_roams_with_varied_brief_checks_and_forward_dwell():
    behavior = HumanBehavior(_GridWorld())
    starts = Counter()
    schedules = []
    both_sides = set()
    for bot_id in range(1, 25):
        actor, frame, intent = roaming_scene(bot_id)
        glances, times, was_glancing = 0, [], False
        for tick in range(320):
            now = 100. + tick / 8
            x = actor.position[0] + tick * .5
            walker = replace(actor, position=(x, actor.position[1], actor.position[2]),
                eye=(x, actor.eye[1], actor.eye[2]))
            movement = replace(intent.movement, travel_source=walker.position,
                travel_waypoint=(x + 20., walker.position[1], walker.position[2]),
                gaze_waypoint=(x + 30., walker.position[1], walker.eye[2]))
            traveling = replace(intent, movement=movement, look=LookIntent(movement.gaze_waypoint))
            reply = behavior.overlay(replace(frame, created_at=now, players=(walker,)), traveling)
            assert reply.movement == movement and reply.action == traveling.action
            checking = reply.social_cue == "roam_check"
            if checking:
                glances += 1
                assert reply.look.glance
                dx, dy = reply.look.target[0] - x, reply.look.target[1] - walker.eye[1]
                assert abs(math.atan2(dy, dx)) <= math.radians(55)
                assert abs(reply.look.target[2] - walker.eye[2]) < 1.5
                both_sides.add(math.copysign(1, dy))
                if not was_glancing:
                    starts[tick] += 1
                    times.append(tick)
            was_glancing = checking
        assert 4 <= len(times) <= 12
        assert 0.05 < glances / 320 < .3
        schedules.append(tuple(times))
    assert len(set(schedules)) >= 20
    assert max(starts.values()) < 12  # No synchronized squad scanning ritual.
    assert both_sides == {-1., 1.}


def test_a_roaming_check_holds_one_visible_point_then_returns_to_the_route():
    behavior, _, frame, intent, first = active_glance()
    again = behavior.overlay(replace(frame, created_at=frame.created_at + .125), intent)
    assert again.look == first.look
    returned = behavior.overlay(replace(frame, created_at=frame.created_at + 1.), intent)
    assert returned == intent
    assert behavior.overlay(replace(frame, created_at=frame.created_at + 1.5), intent) == intent


@pytest.mark.parametrize("reason", (
    "combat", "dig", "reload", "damage", "jump", "drop", "swim", "sneak", "crouch",
    "alignment", "short_step", "raised_step", "airborne", "wade", "carry", "zombie_order",
    "enemy", "hazard", "awareness", "disabled", "ended", "corner", "changed_floor",
))
def test_roaming_attention_immediately_yields_to_a_task_danger_or_precise_step(reason):
    behavior, actor, frame, intent, _ = active_glance()
    frame = replace(frame, created_at=frame.created_at + .125)
    if reason == "combat":
        intent = replace(intent, look=LookIntent((30., 10., 37.), visible=True))
    elif reason in {"dig", "reload"}:
        intent = replace(intent, action=BotAction(
            BotActionKind.MELEE if reason == "dig" else BotActionKind.RELOAD))
    elif reason == "damage":
        actor = replace(actor, last_damage_at=frame.created_at - .1)
    elif reason in {"jump", "drop", "swim"}:
        intent = replace(intent, movement=replace(intent.movement,
            affordance=MovementAffordance(reason)))
    elif reason in {"sneak", "crouch"}:
        intent = replace(intent, movement=replace(intent.movement, **{reason: True}))
    elif reason == "alignment":
        intent = replace(intent, movement=replace(intent.movement, travel_arrival_radius=.025))
    elif reason in {"short_step", "raised_step"}:
        intent = replace(intent, movement=replace(intent.movement, travel_waypoint=(
            actor.position[0] + (1. if reason == "short_step" else 10.),
            actor.position[1], actor.position[2] - (2. if reason == "raised_step" else 0.))))
    elif reason in {"airborne", "wade", "carry"}:
        actor = replace(actor, **({"grounded": False} if reason == "airborne" else
            {"wade": True} if reason == "wade" else {"carried_entity_id": 5}))
    elif reason == "zombie_order":
        frame = replace(frame, objectives=(ObjectiveSnapshot("zombie_order", actor.team,
            (30., 10., 20.), carrier_id=actor.player_id),))
    elif reason == "enemy":
        frame = replace(frame, players=(actor, _fighter(3, 3, 15., 10.5)))
    elif reason == "hazard":
        frame = replace(frame, entities=(EntitySnapshot(1, 1, 3, -1,
            actor.position, hazardous=True),))
    elif reason == "awareness":
        intent = replace(intent, look=LookIntent((15., 10., 20.), glance=True))
    elif reason == "disabled":
        frame = replace(frame, social_interactions=False)
    elif reason == "ended":
        frame = replace(frame, mode_phase="ended")
    elif reason == "corner":
        intent = replace(intent, movement=replace(intent.movement, direction=(0., 1., 0.)))
    else:
        actor = replace(actor, eye=(actor.eye[0], actor.eye[1], actor.eye[2] - 3.))
    if reason != "enemy":
        frame = replace(frame, players=(actor,))
    assert behavior.overlay(frame, intent) == intent
    assert behavior.lives[(actor.player_id, actor.generation)].roaming_look is None


@pytest.mark.parametrize("obstacle", ("wall", "unsupported", "ledge_ahead"))
def test_no_scanning_through_walls_or_while_crossing_unsupported_ground(obstacle):
    world = _GridWorld()
    if obstacle == "wall":
        world.has_line_of_sight = lambda *_args: False
    else:
        original = world.solid
        world.solid = lambda x, y, z: original(x, y, z) and (
            False if obstacle == "unsupported" else x < 12)
    behavior = HumanBehavior(world)
    actor, frame, intent = roaming_scene()
    behavior.overlay(frame, intent)
    due = behavior.lives[(actor.player_id, actor.generation)].next_roam_at
    assert behavior.overlay(replace(frame, created_at=due), intent) == intent


def test_old_life_cannot_resume_a_pending_roaming_look():
    behavior, actor, frame, intent, _ = active_glance()
    reborn = replace(actor, life_id=actor.life_id + 1)
    result = behavior.overlay(replace(frame, created_at=frame.created_at + .125,
        players=(reborn,)), replace(intent, life_id=reborn.life_id))
    assert result.social_cue != "roam_check"
