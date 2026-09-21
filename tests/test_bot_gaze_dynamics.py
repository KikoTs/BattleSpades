"""Purposeful gaze has measurable dwell, smooth turns and precise action escape."""

from dataclasses import replace
import inspect
import math
import random
from types import SimpleNamespace

import pytest

from server.bot_ai.director import BotDirector, _AimMotor, _RuntimeBot
from server.bot_ai.profiles import ProfileFactory


def measure_gaze(case, *, difficulty="normal", dt=.1, seconds=8., purpose="travel"):
    actor = SimpleNamespace(eye_x=0., eye_y=0., eye_z=0.)
    actor.set_orientation_vector = lambda *value: setattr(actor, "orientation", value)
    profile = ProfileFactory(717).create(difficulty)
    runtime = _RuntimeBot(actor, 1, profile, _AimMotor(0.), random.Random(919))
    director = object.__new__(BotDirector)
    supports_purpose = "purpose" in inspect.signature(director._update_aim).parameters
    rows = []
    for tick in range(round(seconds / dt)):
        now = tick * dt
        yaw = (0. if case == "straight" else
               math.radians(1.5 if tick % 2 else -1.5) if case == "small_jitter" else
               math.radians(0. if now < 1. else 90. if now < 4. else 45.))
        kwargs = {"purpose": purpose} if supports_purpose and purpose is not None else {}
        director._update_aim(runtime, (20. * math.cos(yaw), 20. * math.sin(yaw), 0.), dt,
                             **kwargs)
        rows.append((now, runtime.motor.yaw, runtime.motor.yaw_velocity, yaw))
    acceleration = [(rows[i][2] - rows[i - 1][2]) / dt for i in range(1, len(rows))]
    jerk = [abs(acceleration[i] - acceleration[i - 1]) / dt
            for i in range(1, len(acceleration))]
    speeds = [r[2] for r in rows if r[0] >= 1.]
    signs = [math.copysign(1., speed) for speed in speeds if abs(speed) > math.radians(.5)]
    reversal_count = sum(a != b for a, b in zip(signs, signs[1:]))
    dwell = [abs(speed) < math.radians(2.) for speed in speeds]
    run = longest = 0
    for held in dwell:
        run = run + 1 if held else 0
        longest = max(longest, run)
    settled = next((r[0] - 1. for r in rows if 1. <= r[0] < 4.
                    and abs(director._wrap(r[1] - math.pi / 2)) < math.radians(3.)), None)
    ordered_jerk = sorted(jerk)
    metrics = {
        "reversals": reversal_count,
        "jerk_p95_rad_s3": ordered_jerk[int(.95 * (len(ordered_jerk) - 1))],
        "jerk_max_rad_s3": max(jerk),
        "dwell_fraction": sum(dwell) / len(dwell),
        "longest_dwell_seconds": longest * dt,
        "turn_90_settle_seconds": settled,
        "final_error_degrees": math.degrees(abs(director._wrap(rows[-1][1] - rows[-1][3]))),
    }
    return metrics, rows, runtime, director


@pytest.mark.parametrize("difficulty", ("casual", "normal", "hard"))
@pytest.mark.parametrize("purpose", ("travel", "focus", "traverse"))
def test_straight_travel_holds_an_intended_heading_without_random_scanning(difficulty, purpose):
    metrics, *_ = measure_gaze("straight", difficulty=difficulty, purpose=purpose)
    assert metrics["reversals"] == 0
    assert metrics["dwell_fraction"] == 1.
    assert metrics["final_error_degrees"] == pytest.approx(0.)


def test_minor_route_target_jitter_has_dwell_instead_of_alternating_head_turns():
    metrics, *_ = measure_gaze("small_jitter", difficulty="hard")
    assert metrics["reversals"] <= 2
    assert metrics["dwell_fraction"] >= .8
    assert metrics["final_error_degrees"] <= 2.


@pytest.mark.parametrize("difficulty", ("casual", "normal", "hard"))
def test_real_route_corners_turn_smoothly_without_abandoning_the_direction(difficulty):
    metrics, rows, *_ = measure_gaze("corners", difficulty=difficulty)
    # Movement keys follow the view, so a corner taken as a two-second pan left
    # the body strafing on the spot. Players snap to the new heading.
    assert metrics["turn_90_settle_seconds"] <= .5
    assert metrics["final_error_degrees"] < 2.
    assert metrics["reversals"] <= 2  # Only the intentional 90 -> 45 degree corner.
    assert max(row[1] for row in rows[:40]) <= math.pi / 2 + math.radians(.5)


def test_a_short_exact_step_is_followed_at_once_and_as_quickly_as_a_corner():
    metrics, rows, *_ = measure_gaze("corners", purpose="traverse")
    assert metrics["turn_90_settle_seconds"] <= .5
    assert max(row[1] for row in rows[:40]) <= math.pi / 2 + math.radians(.5)


@pytest.mark.parametrize("dt", (1 / 60, .1, .25))
def test_corner_turns_never_overshoot_at_any_motor_cadence(dt):
    _, rows, *_ = measure_gaze("corners", dt=dt)
    assert max(row[1] for row in rows if row[0] < 4.) <= math.pi / 2 + math.radians(.5)


@pytest.mark.parametrize("purpose", ("combat", "precise", "traverse"))
def test_combat_and_world_actions_immediately_take_aim_ownership_from_travel_dwell(purpose):
    _, _, runtime, director = measure_gaze("straight", seconds=2.)
    runtime.profile = replace(runtime.profile, aim_noise=0.)
    director._update_aim(runtime, (20., 1., 0.), .1, purpose=purpose)
    assert runtime.motor.yaw > math.radians(.5)


def test_combat_mode_retains_the_existing_difficulty_and_noise_contract():
    explicit = measure_gaze("corners", purpose="combat")[1]
    default = measure_gaze("corners", purpose=None)[1]
    assert explicit == default


def test_purposeful_turns_are_reproducible_from_the_same_personality():
    assert measure_gaze("corners")[1] == measure_gaze("corners")[1]
