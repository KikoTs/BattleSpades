"""Heads move at a human pace, never freeze while waiting, and never in step."""
import collections
from dataclasses import replace
import math
import random
from types import SimpleNamespace

import pytest
import shared.constants as C

from server.bot_ai.director import BotDirector, _AimMotor, _RuntimeBot
from server.bot_ai.human_behavior import HumanBehavior
from server.bot_ai.messages import (
    BotAction, BotActionKind, BotIntentPriority, LookIntent, MovementAffordance,
    MovementIntent, ObjectiveSnapshot, Stimulus, StimulusKind,
)
from server.bot_ai.profiles import ProfileFactory
from tests.test_bot_combat_tactics import _GridWorld, _fighter
from tests.test_bot_social_behavior import scene


# -- the aim motor -----------------------------------------------------------

def motor_rig(difficulty="normal", seed=717):
    actor = SimpleNamespace(eye_x=0., eye_y=0., eye_z=0.)
    actor.set_orientation_vector = lambda *value: setattr(actor, "orientation", value)
    profile = ProfileFactory(seed).create(difficulty)
    runtime = _RuntimeBot(actor, 1, profile, _AimMotor(0.), random.Random(919))
    return object.__new__(BotDirector), runtime


def drive(director, runtime, schedule, *, dt=1 / 30):
    """Run the 30 Hz motor through (seconds, bearing_deg | None, purpose) legs.

    Returns 10 Hz samples of (t, yaw) like the match trace, plus the yaw after
    every motor tick.
    """
    rows, t = [], 0.
    for seconds, bearing, purpose in schedule:
        for _ in range(round(seconds / dt)):
            if bearing is None:
                director._settle_aim(runtime, dt)
            else:
                yaw = math.radians(bearing(t) if callable(bearing) else bearing)
                director._update_aim(runtime, (20. * math.cos(yaw), 20. * math.sin(yaw), 0.),
                                     dt, purpose=purpose)
            t += dt
            rows.append((t, runtime.motor.yaw, runtime.motor.gaze_yaw))
    sampled = rows[::3]
    return sampled, rows


def steps_deg(director, sampled):
    return [math.degrees(abs(director._wrap(b[1] - a[1]))) for a, b in zip(sampled, sampled[1:])]


@pytest.mark.parametrize("difficulty", ("casual", "normal", "hard"))
@pytest.mark.parametrize("purpose", ("focus", "glance"))
def test_relaxed_looks_turn_the_head_at_a_human_pace_without_overshoot(difficulty, purpose):
    director, runtime = motor_rig(difficulty)
    sampled, rows = drive(director, runtime, ((1., 0., purpose), (2., 60., purpose)))
    assert max(steps_deg(director, sampled)) <= 19.          # <= 190 deg/s per 100 ms sample
    assert all(-math.radians(1.) <= yaw <= math.radians(61.) for _, yaw, _ in rows)  # no overshoot
    assert abs(director._wrap(rows[-1][1] - math.radians(60.))) < math.radians(1.)
    # A look right round over the shoulder is quicker, as a real one is, but
    # still bounded and still without overshoot.
    director, runtime = motor_rig(difficulty)
    sampled, rows = drive(director, runtime, ((1., 0., purpose), (2., 170., purpose)))
    assert 19. < max(steps_deg(director, sampled)) <= 36.
    assert all(-math.radians(1.) <= yaw <= math.radians(171.) for _, yaw, _ in rows)
    assert abs(director._wrap(rows[-1][1] - math.radians(170.))) < math.radians(1.)


@pytest.mark.parametrize("difficulty", ("casual", "normal", "hard"))
def test_steering_turns_stay_quick_but_never_swing_half_a_right_angle_per_sample(difficulty):
    director, runtime = motor_rig(difficulty)
    sampled, rows = drive(director, runtime, ((1., 0., "travel"), (2., 179., "travel")))
    assert max(steps_deg(director, sampled)) < 50.
    settled = next(t - 1. for t, yaw, _ in rows if t >= 1. and abs(director._wrap(yaw - math.radians(179.))) < math.radians(3.))
    assert settled <= .8
    director, runtime = motor_rig(difficulty)
    sampled, rows = drive(director, runtime, ((1., 0., "travel"), (1., 90., "travel")))
    assert next(t - 1. for t, yaw, _ in rows if t >= 1.
                and abs(director._wrap(yaw - math.pi / 2)) < math.radians(3.)) <= .5


def test_route_wobble_is_not_followed_but_a_change_that_persists_is():
    director, runtime = motor_rig()
    wobble = lambda t: 45. if int(t * 10) % 2 else -10.
    sampled, rows = drive(director, runtime, ((1., 0., "travel"), (4., wobble, "travel")))
    commits = sum(1 for a, b in zip(rows, rows[1:]) if a[2] != b[2] and a[0] >= 1.)
    assert commits <= 10                                          # not one per wobble (40)
    signs = [math.copysign(1., director._wrap(b[1] - a[1])) for a, b in zip(sampled, sampled[1:])
             if abs(director._wrap(b[1] - a[1])) > math.radians(1.15)]
    assert sum(a != b for a, b in zip(signs, signs[1:])) <= 12
    # A new heading right after a corner is adopted once it stays put.
    director, runtime = motor_rig()
    _, rows = drive(director, runtime, ((1., 0., "travel"), (.2, 60., "travel"), (1.5, -30., "travel")))
    adopted = next(t for t, _, gaze in rows if t > 1.2 and gaze is not None
                   and abs(director._wrap(gaze - math.radians(-30.))) < 1e-6)
    assert adopted - 1.2 <= .4
    assert abs(director._wrap(rows[-1][1] - math.radians(-30.))) < math.radians(2.)


def test_return_from_a_glance_is_eased_rather_than_flicked_back_to_the_route():
    director, runtime = motor_rig("hard")
    sampled, rows = drive(director, runtime, (
        (1., 0., "travel"), (.9, 40., "glance"), (1.5, 0., "travel")))
    out = [s for s in steps_deg(director, sampled[10:19])]
    back = [s for s in steps_deg(director, sampled[19:])]
    assert max(out) <= 19. and max(back) <= 27.
    assert abs(rows[-1][1]) < math.radians(1.5)


def test_a_frame_without_a_look_finishes_the_turn_instead_of_freezing_mid_swing():
    director, runtime = motor_rig()
    # Commit a corner, then the worker sends two frames without any look.
    drive(director, runtime, ((1., 0., "travel"), (.1, 90., "travel")))
    before = runtime.motor.yaw
    assert abs(runtime.motor.yaw_velocity) > math.radians(30.)
    sampled, rows = drive(director, runtime, ((1.5, None, ""),))
    assert rows[0][1] > before + math.radians(2.)               # kept turning
    assert all(b[1] >= a[1] - 1e-9 for a, b in zip(rows, rows[1:]))  # monotone, no snap back
    assert abs(runtime.motor.yaw - math.pi / 2) < math.radians(2.)
    assert runtime.motor.yaw_velocity == 0. and runtime.motor.gaze_yaw is None  # came to rest and let go
    assert max(steps_deg(director, sampled)) <= 19.


# -- waiting, working and listening ------------------------------------------

def standing(actor_id=1, now=100., **changes):
    actor, human, frame, intent = scene(actor_id=actor_id, now=now)
    actor = replace(actor, **changes)
    watch = LookIntent((actor.eye[0] + 10., actor.eye[1], actor.eye[2]))
    return actor, replace(frame, players=(actor,)), replace(intent, look=watch, debug_role="post")


def bearing(actor, look):
    return math.degrees(math.atan2(look.target[1] - actor.eye[1], look.target[0] - actor.eye[0]))


def test_a_waiting_bot_breathes_slowly_around_its_watch_point():
    behavior = HumanBehavior(_GridWorld())
    actor, frame, intent = standing()
    assert behavior.overlay(frame, intent) == intent
    assert behavior.overlay(replace(frame, created_at=100.5), intent) == intent  # still settling
    bearings, cues = [], set()
    for tick in range(30, 110):
        reply = behavior.overlay(replace(frame, created_at=100. + tick / 10), intent)
        cues.add(reply.social_cue)
        assert reply.movement == intent.movement and reply.action == intent.action
        assert reply.look.glance
        bearings.append(bearing(actor, reply.look))
    assert cues == {"standing_scan"}
    assert max(abs(b) for b in bearings) <= 17.                      # a few degrees either side
    assert max(abs(b - a) for a, b in zip(bearings, bearings[1:])) <= 2.6  # slow, no jumps
    assert min(bearings) < -3. < 3. < max(bearings)                  # actually moves both ways


def test_sixteen_waiting_bots_do_not_scan_in_step():
    behavior = HumanBehavior(_GridWorld())
    onsets, series = [], []
    for bot_id in range(1, 17):
        actor, frame, intent = standing(bot_id)
        offsets, onset = [], None
        for tick in range(0, 120):
            reply = behavior.overlay(replace(frame, created_at=100. + tick / 10), intent)
            if reply.social_cue == "standing_scan" and onset is None:
                onset = tick
            offsets.append(bearing(actor, reply.look) if reply.look is not None else 0.)
        onsets.append(onset)
        series.append(offsets[40:])
    assert len(set(onsets)) >= 10
    # At any one moment some look left and some right of their point...
    at_108 = [s[40] for s in series]
    assert 3 <= sum(1 for offset in at_108 if offset > 0.) <= 13
    # ...the moments they swing back through it differ...
    crossings = [next((k for k, (a, b) in enumerate(zip(s, s[1:])) if a > 1. and b <= 1.), None)
                 for s in series]
    assert len(set(crossings)) >= 8 and max(collections.Counter(crossings).values()) <= 3
    # ...and no two heads trace the same path.
    lockstep = sum(1 for i in range(16) for j in range(i + 1, 16)
                   if sum(1 for a, b in zip(series[i], series[j]) if abs(a - b) < 1.) > .9 * len(series[i]))
    assert lockstep == 0


def test_siege_zombie_scans_while_watching_a_survivor_out_of_claw_reach():
    behavior = HumanBehavior(_GridWorld())
    actor, frame, intent = standing(class_id=int(C.CLASS_ZOMBIE))
    survivor = _fighter(3, 3, 30.5, 10.5)
    order = ObjectiveSnapshot("zombie_order", actor.team, (12.5, 10.5, 37.75),
                              carrier_id=actor.player_id, state=3)
    frame = replace(frame, mode_id="zom", players=(actor, survivor), objectives=(order,))
    intent = replace(intent, priority=BotIntentPriority.TRAVERSAL, debug_role="zombie_siege_climb")
    behavior.overlay(frame, intent)
    reply = behavior.overlay(replace(frame, created_at=103.5), intent)
    assert reply.social_cue == "standing_scan" and reply.movement == intent.movement
    # The same survivor within claw reach is business, not scenery.
    close = replace(frame, players=(actor, _fighter(3, 3, 14.5, 10.5)), created_at=103.6)
    assert behavior.overlay(close, intent) == intent
    # A survivor (not infected) seeing an enemy that far is a fight.
    soldier = HumanBehavior(_GridWorld())
    actor2, frame2, intent2 = standing()
    frame2 = replace(frame2, players=(actor2, survivor))
    soldier.overlay(frame2, intent2)
    assert soldier.overlay(replace(frame2, created_at=103.5), intent2) == intent2


def hunting_zombie(now=100.):
    actor, frame, intent = standing(class_id=int(C.CLASS_ZOMBIE), now=now)
    movement = MovementIntent(direction=(1., 0., 0.), travel_source=actor.position,
        travel_waypoint=(actor.position[0] + 20., actor.position[1], actor.position[2]),
        gaze_waypoint=(actor.position[0] + 30., actor.position[1], actor.eye[2]))
    order = ObjectiveSnapshot("zombie_order", actor.team, (40.5, 10.5, 37.75),
                              carrier_id=actor.player_id, state=0)
    frame = replace(frame, mode_id="zom", objectives=(order,))
    intent = replace(intent, movement=movement, look=LookIntent(movement.gaze_waypoint),
                     priority=BotIntentPriority.TRAVERSAL, debug_role="zombie_hunt_survivor")
    return actor, frame, intent


def test_hunting_zombie_turns_briefly_to_gunfire_then_back_to_the_work():
    behavior = HumanBehavior(_GridWorld())
    actor, frame, intent = hunting_zombie()
    assert behavior.overlay(frame, intent) == intent
    for tick in range(1, 30):   # no side checks on a hunt order
        assert behavior.overlay(replace(frame, created_at=100. + tick / 10), intent) == intent
    shot = Stimulus(StimulusKind.SHOT, (10.5, 30.5, actor.eye[2]), 103.05, 104.3, source_id=9, team=3)
    cues = []
    for tick in range(0, 20):
        now = 103.1 + tick / 10
        reply = behavior.overlay(replace(frame, created_at=now, stimuli=(shot,) if now < 104.3 else ()), intent)
        cues.append(reply.social_cue)
        assert reply.movement == intent.movement and reply.action == intent.action
        if reply.social_cue:
            assert reply.look.glance and reply.look.target[:2] == shot.position[:2]
    glanced = [c for c in cues if c == "sound_glance"]
    assert 3 <= len(glanced) <= 10 and cues[0] == "" and cues[-1] == ""
    again = Stimulus(StimulusKind.SHOT, (10.5, 30.5, actor.eye[2]), 105.5, 106.7, source_id=9, team=3)
    assert all(behavior.overlay(replace(frame, created_at=105.6 + t / 10, stimuli=(again,)), intent) == intent
               for t in range(8))


def test_a_shot_from_where_the_zombie_already_looks_or_from_its_own_side_is_no_news():
    for team, position in ((3, (40.5, 10.5, 37.75)), (2, (10.5, 30.5, 37.75))):
        behavior = HumanBehavior(_GridWorld())
        actor, frame, intent = hunting_zombie()
        behavior.overlay(frame, intent)
        shot = Stimulus(StimulusKind.SHOT, position, 100.05, 101.3, source_id=9, team=team)
        assert all(behavior.overlay(replace(frame, created_at=100.1 + t / 10, stimuli=(shot,)), intent) == intent
                   for t in range(10))


def test_zombie_glances_once_at_a_teammates_fresh_placement():
    behavior = HumanBehavior(_GridWorld())
    actor, frame, intent = hunting_zombie()
    builder = replace(_fighter(5, 2, 14.5, 13.5), is_bot=True, class_id=int(C.CLASS_ZOMBIE),
                      last_action_kind="place_prefab", last_action_accepted=True,
                      last_action_at=99.9, last_action_position=(16., 14., 38.))
    frame = replace(frame, players=(actor, builder))
    behavior.overlay(frame, intent)
    replies = [behavior.overlay(replace(frame, created_at=100.1 + t / 10), intent)
               for t in range(12)]
    cues = [reply.social_cue for reply in replies]
    assert "teammate_work" in cues
    reply = replies[cues.index("teammate_work")]
    assert reply.look.target == (16.5, 14.5, 38.5) and reply.movement == intent.movement
    # The same placement is not admired twice, nor a rejected one at all.
    assert all(behavior.overlay(replace(frame, created_at=110. + t / 10), intent) == intent
               for t in range(10))
    rejected = replace(builder, last_action_accepted=False, last_action_at=120.)
    fresh = HumanBehavior(_GridWorld())
    frame = replace(frame, players=(actor, rejected), created_at=120.)
    assert all(fresh.overlay(replace(frame, created_at=120.1 + t / 10), intent) == intent
               for t in range(10))


def test_waiting_zombie_checks_the_spot_it_was_given():
    behavior = HumanBehavior(_GridWorld())
    actor, frame, intent = standing(class_id=int(C.CLASS_ZOMBIE))
    order = ObjectiveSnapshot("zombie_order", actor.team, (22.5, 16.5, 37.75),
                              carrier_id=actor.player_id, state=4)
    frame = replace(frame, mode_id="zom", objectives=(order,))
    cues = [behavior.overlay(replace(frame, created_at=100. + t / 10), intent).social_cue
            for t in range(8)]
    assert "order_glance" in cues and cues[0] == ""


@pytest.mark.parametrize("reason", ("swing", "placement", "jump", "aim", "airborne", "hurt"))
def test_scans_and_beats_never_interrupt_a_swing_placement_jump_or_aim(reason):
    behavior = HumanBehavior(_GridWorld())
    actor, frame, intent = standing(class_id=int(C.CLASS_ZOMBIE))
    order = ObjectiveSnapshot("zombie_order", actor.team, (22.5, 16.5, 37.75),
                              carrier_id=actor.player_id, state=2)
    frame = replace(frame, mode_id="zom", objectives=(order,),
                    stimuli=(Stimulus(StimulusKind.SHOT, (10.5, 30.5, actor.eye[2]), 100.05, 104.3, team=3),))
    behavior.overlay(frame, intent)
    frame = replace(frame, created_at=103.5)
    if reason == "swing":
        intent = replace(intent, action=BotAction(BotActionKind.MELEE))
    elif reason == "placement":
        intent = replace(intent, movement=MovementIntent(affordance=MovementAffordance.PLACE_PREFAB))
    elif reason == "jump":
        intent = replace(intent, movement=MovementIntent(jump=True))
    elif reason == "aim":
        intent = replace(intent, look=LookIntent((30.5, 10.5, 37.75), visible=True))
    elif reason == "airborne":
        frame = replace(frame, players=(replace(actor, grounded=False),))
    else:
        frame = replace(frame, players=(replace(actor, last_damage_at=103.2),))
    assert behavior.overlay(frame, intent) == intent
    assert behavior.lives[(actor.player_id, actor.generation)].gesture is None


def test_a_standing_bot_with_nothing_to_watch_still_looks_about_whatever_its_role():
    behavior = HumanBehavior(_GridWorld())
    actor, _, frame, intent = scene()
    frame = replace(frame, players=(actor,))
    intent = replace(intent, debug_role="schematic_wait")
    behavior.overlay(frame, intent)
    assert behavior.overlay(replace(frame, created_at=100.8), intent) == intent
    reply = behavior.overlay(replace(frame, created_at=106.), intent)
    assert reply.social_cue == "idle_look" and reply.look.glance
    walking = replace(intent, movement=MovementIntent(direction=(1., 0., 0.)))
    assert behavior.overlay(replace(frame, created_at=106.1), walking).social_cue != "idle_look"
