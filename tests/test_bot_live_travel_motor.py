"""Explicit route metadata removes snapshot-lag steering and pitch errors."""

from dataclasses import replace
import math
from types import SimpleNamespace

import pytest

from server.bot_ai.director import BotDirector
from server.bot_ai.messages import BotAction, BotActionKind, BotIntent, LookIntent, MovementAffordance, MovementIntent
from tests.test_bot_architecture import _facing_fixture


def travel_intent(*, source=(0., 0., 20.), waypoint=(4., 0., 20.), direction=(1., 0., 0.),
                  affordance=MovementAffordance.WALK):
    return BotIntent(0, 1, 1, 1, 1, 0, 100., 101.,
                     MovementIntent(direction=direction, affordance=affordance,
                                    travel_source=source, travel_waypoint=waypoint),
                     look=LookIntent((source[0] + 6., source[1], source[2])))


def actor(position):
    return SimpleNamespace(player=SimpleNamespace(x=position[0], y=position[1], z=position[2]))


def test_native_egypt_134_9_second_overshoot_releases_instead_of_driving_away():
    source = (272.457, 235.935, 215.743)
    waypoint = (272.5, 237.5, 215.75)
    delta = (waypoint[0] - source[0], waypoint[1] - source[1])
    length = math.hypot(*delta)
    intent = travel_intent(source=source, waypoint=waypoint,
                           direction=(delta[0] / length, delta[1] / length, 0.))
    runtime = actor((272.455, 238.143, 215.743))
    assert BotDirector._travel_movement_direction(runtime, intent) == (0., 0., 0.)


def test_delayed_diagonal_route_steers_from_current_body_not_old_snapshot():
    source = (210.441, 231.798, 227.748)
    waypoint = (211.5, 231.68, 227.75)
    delta = (waypoint[0] - source[0], waypoint[1] - source[1])
    length = math.hypot(*delta)
    intent = travel_intent(source=source, waypoint=waypoint,
                           direction=(delta[0] / length, delta[1] / length, 0.))
    position = (210.007, 229.252, 227.748)
    result = BotDirector._travel_movement_direction(actor(position), intent)
    live = (waypoint[0] - position[0], waypoint[1] - position[1])
    live_length = math.hypot(*live)
    assert result[:2] == pytest.approx((live[0] / live_length, live[1] / live_length))
    assert result[1] > .8 and intent.movement.direction[1] < 0.


def test_crowd_separation_keeps_its_rotation_and_strength_during_live_refresh():
    angle = math.radians(20.)
    intent = travel_intent(direction=(math.cos(angle) * .7, math.sin(angle) * .7, 0.))
    result = BotDirector._travel_movement_direction(actor((0., -2., 20.)), intent)
    assert math.hypot(*result[:2]) == pytest.approx(.7)
    assert math.atan2(result[1], result[0]) == pytest.approx(math.atan2(2., 4.) + angle)


@pytest.mark.parametrize("case", ("missing", "combat", "terrain", "flight", "swim"))
def test_unrelated_movement_and_exact_action_targets_are_not_refreshed(case):
    intent = travel_intent()
    if case == "missing":
        intent = replace(intent, movement=replace(intent.movement, travel_source=None))
    elif case == "combat":
        intent = replace(intent, look=replace(intent.look, visible=True))
    elif case == "terrain":
        intent = replace(intent, action=BotAction(BotActionKind.MELEE, position=(1., 0., 22.)))
    else:
        intent = replace(intent, movement=replace(intent.movement, affordance=(
            MovementAffordance.JETPACK if case == "flight" else MovementAffordance.SWIM)))
    assert BotDirector._travel_movement_direction(actor((0., -2., 20.)), intent) == intent.movement.direction


def test_passed_destination_on_a_lower_floor_is_not_treated_as_arrival():
    intent = travel_intent()
    result = BotDirector._travel_movement_direction(actor((4.5, 0., 23.)), intent)
    assert result == pytest.approx((-1., 0., 0.))


@pytest.mark.parametrize("pending", (False, True))
def test_live_route_gaze_stays_level_after_native_height_change_but_preserves_action_aim(pending):
    _, director, bot, runtime = _facing_fixture()
    runtime.profile = replace(runtime.profile, aim_noise=0.)
    eye = bot.eye
    source = (eye[0], eye[1], eye[2] + 3.)
    runtime.intent = travel_intent(source=source, waypoint=(eye[0] + 5., eye[1], eye[2]))
    if pending:
        runtime.pending_action = BotAction(BotActionKind.MELEE, position=(eye[0] + 2., eye[1], eye[2] + 3.))
        runtime.pending_action_deadline = 101.
    director._apply_motor(runtime, 100., .1)
    if pending:
        assert runtime.motor.pitch > .01
    else:
        assert runtime.motor.pitch == pytest.approx(0.)


def test_live_refreshed_heading_still_passes_through_authoritative_collision_probe(monkeypatch):
    _, director, bot, runtime = _facing_fixture()
    eye = bot.eye
    runtime.intent = travel_intent(source=(eye[0], eye[1] + 2., eye[2]),
                                  waypoint=(eye[0] + 4., eye[1] + 2., eye[2]))
    observed = []
    def blocked(_runtime, direction, affordance, **kwargs):
        observed.append(direction)
        return (0., 0., 0.)
    monkeypatch.setattr(director, "_live_movement_direction", blocked)
    director._apply_motor(runtime, 100., .1)
    assert observed[0][1] > .4
    assert not any(runtime.movement_input[:4])


@pytest.mark.parametrize("offset,purpose", (
    ((-1., -.181, -1.), "traverse"),  # London shelf's immediate raised step.
    ((1., 0., 0.), "traverse"),
    ((6., 0., 0.), "travel"),
))
def test_short_native_steps_acquire_heading_promptly_while_long_routes_keep_gaze_dwell(offset, purpose):
    _, director, bot, runtime = _facing_fixture()
    source = tuple(bot.position)
    waypoint = tuple(value + delta for value, delta in zip(source, offset))
    length = math.hypot(*offset[:2])
    runtime.intent = travel_intent(source=source, waypoint=waypoint,
                                   direction=(offset[0] / length, offset[1] / length, 0.))
    director._apply_motor(runtime, 100., .1)
    assert runtime.motor.gaze_purpose == purpose
    assert runtime.motor.yaw_noise == 0.
    assert runtime.motor.pitch_noise == 0.


@pytest.mark.parametrize("degrees", (0., 10., 20., 33., 45., 70., 100., 180., -135.))
def test_movement_keys_go_where_asked_wherever_the_eyes_look(degrees):
    """Independent axis thresholds turned 20 degrees off the view into a 45 degree strafe."""
    runtime = SimpleNamespace(key_residual=(0., 0.))
    forward, side = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    total = [0., 0.]
    for _ in range(40):
        keys = BotDirector._movement_keys(runtime, forward, side)
        assert keys != (0, 0)
        length = math.hypot(*keys)
        total[0] += keys[0] / length
        total[1] += keys[1] / length
    heading = math.degrees(math.atan2(total[1], total[0]))
    assert abs((heading - degrees + 180.) % 360. - 180.) < 2.5
    if degrees == 0.:
        assert total == [40., 0.]  # straight ahead is plain W, never a feathered strafe


def test_no_requested_movement_presses_nothing_and_forgets_the_old_heading():
    runtime = SimpleNamespace(key_residual=(.4, -.3))
    assert BotDirector._movement_keys(runtime, 0., 0.) == (0, 0)
    assert runtime.key_residual == (0., 0.)


class _LedgeWorld:
    """Floor at support 40; columns in ``lower`` sit that many blocks down."""

    topology_version = 1

    def __init__(self, lower=None, water=()):
        self.lower, self.water = dict(lower or {}), set(water)

    def _floor(self, x, y):
        return 40 + self.lower.get((int(math.floor(x)), int(math.floor(y))), 0)

    def get_solid(self, x, y, z):
        return z >= self._floor(x, y)

    def clipbox(self, x, y, z):
        return math.floor(z) >= self._floor(x, y)

    def is_water_column(self, x, y):
        return (x, y) in self.water


def _walker(world, allowance):
    player = SimpleNamespace(x=10.5, y=10.5, z=40 - 2.25, vx=0., vy=0., wade=False, grounded=True,
                             connection=SimpleNamespace(server=SimpleNamespace(world_manager=world)))
    return SimpleNamespace(player=player, waypoint_probe_key=None, waypoint_probe_result=False,
                           walk_drop_allowance=allowance)


def test_a_walk_runs_off_a_small_ledge_only_when_the_worker_validated_one():
    ledge = _LedgeWorld({(11, y): 3 for y in range(8, 14)})
    assert not BotDirector._waypoint_is_live(_walker(ledge, 1), (1., 0., 0.))
    assert BotDirector._waypoint_is_live(_walker(ledge, 4), (1., 0., 0.))
    cliff = _LedgeWorld({(11, y): 7 for y in range(8, 14)})
    assert not BotDirector._waypoint_is_live(_walker(cliff, 4), (1., 0., 0.))
    shore = _LedgeWorld({(11, y): 3 for y in range(8, 14)}, water={(11, y) for y in range(8, 14)})
    assert not BotDirector._waypoint_is_live(_walker(shore, 4), (1., 0., 0.))
    # One shoulder over a deeper neighbouring step does not veto the centre's landing.
    terrace = _LedgeWorld({**{(11, y): 3 for y in range(8, 14)}, (11, 11): 9})
    assert BotDirector._waypoint_is_live(_walker(terrace, 4), (1., 0., 0.))
    assert not BotDirector._waypoint_is_live(_walker(terrace, 1), (1., 0., 0.))
