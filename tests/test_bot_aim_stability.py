"""Actual motor cadence must not manufacture camera oscillation or stale aim."""

from dataclasses import replace
import math
import random
from types import SimpleNamespace

import pytest
import shared.constants as C

from server.bot_ai.director import BotDirector, _AimMotor, _RuntimeBot
from server.bot_ai.messages import BotAction, BotActionKind, BotIntent, LookIntent, MovementIntent
from tests.test_bot_architecture import _facing_fixture, _profile


def aim_fixture(yaw=0.):
    actor = SimpleNamespace(eye_x=10., eye_y=10., eye_z=10.)
    actor.set_orientation_vector = lambda x, y, z: setattr(actor, "orientation", (x, y, z))
    profile = replace(_profile(), aim_noise=0.)
    state = _RuntimeBot(actor, 1, profile, _AimMotor(yaw), random.Random(1))
    return object.__new__(BotDirector), state


@pytest.mark.parametrize("dt", (.1, .125, .2, .25))
def test_fixed_pitch_converges_without_staggered_tick_oscillation(dt):
    director, state = aim_fixture()
    pitch = .4
    target = (20., 10., 10. + 10. * math.tan(pitch))
    samples = []
    for _ in range(round(3. / dt)):
        director._update_aim(state, target, dt)
        samples.append(state.motor.pitch)
    # Prior single-step integration peaked at .5625 for a .4 target on a
    # 250 ms tick and crossed it 18 times within 5 s, with no target movement.
    assert all(first <= second + 1e-10 for first, second in zip(samples, samples[1:]))
    assert max(samples) <= pitch + 1e-8
    assert samples[-1] == pytest.approx(pitch, abs=1e-5)


def test_ten_hz_motor_matches_sixty_hz_angular_dynamics_for_the_same_fixed_target():
    director, slow = aim_fixture()
    _, fast = aim_fixture()
    target = (10., 20., 14.)
    for _ in range(10):
        director._update_aim(slow, target, .1)
        for _ in range(6):
            director._update_aim(fast, target, 1. / 60.)
        assert slow.motor.yaw == pytest.approx(fast.motor.yaw, abs=1e-10)
        assert slow.motor.pitch == pytest.approx(fast.motor.pitch, abs=1e-10)


def test_vertical_target_does_not_turn_toward_an_arbitrary_horizontal_heading():
    director, state = aim_fixture(yaw=1.1)
    for _ in range(20):
        director._update_aim(state, (10., 10., -90.), .1)
    assert state.motor.yaw == pytest.approx(1.1)


def test_pitch_limit_does_not_store_outward_velocity_or_delay_looking_away():
    director, state = aim_fixture()
    for _ in range(30):
        director._update_aim(state, (10., 10., -90.), .1)
    previous = state.motor.pitch
    assert previous == pytest.approx(-1.35, abs=1e-5)
    assert abs(state.motor.pitch_velocity) < 1e-5
    director._update_aim(state, (20., 10., 10.), .1)
    assert state.motor.pitch > previous + .01


def test_wrapped_yaw_takes_the_short_path_at_the_real_staggered_cadence():
    director, state = aim_fixture(yaw=math.radians(179.))
    goal = math.radians(-179.)
    target = (10. + 10. * math.cos(goal), 10. + 10. * math.sin(goal), 10.)
    previous = state.motor.yaw
    total = 0.
    for _ in range(10):
        director._update_aim(state, target, .1)
        step = director._wrap(state.motor.yaw - previous)
        assert step >= -1e-10
        total += step
        previous = state.motor.yaw
    assert total == pytest.approx(math.radians(2.), abs=1e-4)


@pytest.mark.parametrize("fresh_intent", (False, True))
def test_expired_pending_action_cannot_steer_camera_before_being_cleared(fresh_intent):
    _, director, bot, state = _facing_fixture()
    state.profile = replace(state.profile, aim_noise=0.)
    now = 100.
    state.pending_action = BotAction(BotActionKind.MELEE, int(C.SUPERSPADE_TOOL),
                                    position=(bot.eye_x + 1., bot.eye_y, bot.eye_z + 10.))
    state.pending_action_deadline = now - .01
    state.intent = (BotIntent(bot.id, state.generation, 1, 1, 1, 0, now, now + 1.,
                            MovementIntent(),
                            look=LookIntent((bot.eye_x + 20., bot.eye_y, bot.eye_z)))
                    if fresh_intent else None)
    director._apply_motor(state, now, .1)
    assert state.pending_action is None
    assert state.motor.pitch == pytest.approx(0.)
    assert bot.o_z == pytest.approx(0.)


@pytest.mark.parametrize("planning_wait", (False, True))
def test_held_look_drops_old_angular_momentum_before_travel_resumes(planning_wait):
    _, director, bot, state = _facing_fixture()
    state.profile = replace(state.profile, aim_noise=0.)
    state.motor.pitch = math.radians(-1.8)
    state.motor.pitch_velocity = -3.
    state.motor.yaw_velocity = 2.
    now = 100.
    wait = BotIntent(bot.id, state.generation, 1, 1, 1, 0, now, now + 1.,
                     MovementIntent(), debug_role="team_assault_enemy_side:planning_wait")
    state.intent = wait if planning_wait else None
    director._apply_motor(state, now, .1)
    held_pitch, held_yaw = state.motor.pitch, state.motor.yaw
    assert state.motor.pitch_velocity == state.motor.yaw_velocity == 0.
    state.intent = replace(wait, frame_id=2, debug_role="team_assault_enemy_side",
                           look=LookIntent((bot.eye_x + 6., bot.eye_y, bot.eye_z)))
    director._apply_motor(state, now + .1, .1)
    # AncientEgypt trace bot7 previously jumped from -1.8 to -14.1 degrees
    # after planning_wait because the old breach's velocity resumed.
    assert held_pitch < state.motor.pitch <= 0.
    assert state.motor.yaw == pytest.approx(held_yaw)
