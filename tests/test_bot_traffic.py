"""Local yielding cannot trade a valid bridge for an unsupported side step."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from server.bot_ai.traffic import Traffic
from server.bot_ai.messages import BotIntentPriority, MovementAffordance, MovementIntent
from tests.test_bot_social_behavior import scene


class Floor:
    def __init__(self, *, bridge=False, start=0):
        self.bridge, self.start = bridge, start

    def solid(self, x, y, z):
        return z >= 40 and x >= self.start and (not self.bridge or y == 10)

    def surface(self, x, y, z, **_kwargs):
        return SimpleNamespace(support_z=40) if self.solid(x, y, 40) else None


def walking(intent, actor, heading=(1., 0., 0.)):
    return replace(intent, movement=MovementIntent(heading, sprint=True,
        travel_source=actor.position, travel_waypoint=(30., 10.5, 37.75)))


def test_one_block_bridge_keeps_its_centerline_when_neighbors_repel_sideways():
    actor, _, frame, intent = scene()
    frame = replace(frame, players=(actor,))
    result = Traffic(Floor(bridge=True)).adjust(frame, walking(intent, actor, (1., .3, 0.)))
    assert result.movement.direction[1] == 0
    assert not result.movement.sprint and result.movement.sneak


@pytest.mark.parametrize("room_behind", (False, True))
def test_human_gets_room_without_either_body_being_pushed_off_an_edge(room_behind):
    actor, human, frame, intent = scene()
    human = replace(human, position=(11.5, 10.5, 37.75))
    frame = replace(frame, players=(actor, human))
    traffic = Traffic(Floor(bridge=True, start=0 if room_behind else 10))
    result = traffic.adjust(frame, walking(intent, actor))
    assert result.social_cue == "yield_human"
    assert result.movement.direction == ((-1., 0., 0.) if room_behind else (0., 0., 0.))
    assert frame.players[1] == human


def test_two_opposing_bots_have_one_stable_right_of_way():
    traffic = Traffic(Floor(bridge=True))
    actor, human, frame, intent = scene(actor_id=2)
    other = replace(human, player_id=1, is_bot=True, position=(12., 10.5, 37.75))
    frame = replace(frame, players=(actor, other))
    first = traffic.adjust(frame, walking(intent, actor))
    assert first.social_cue == "yield_passage"
    for now in (100.1, 100.2, 100.3):
        again = traffic.adjust(replace(frame, created_at=now), walking(intent, actor))
        assert again.movement.direction == first.movement.direction


def test_unchanged_leader_creates_a_queue_instead_of_lateral_shoving():
    actor, human, frame, intent = scene()
    other = replace(human, is_bot=True, position=(11.1, 10.5, 37.75), orientation=(1., 0., 0.))
    result = Traffic(Floor(bridge=True)).adjust(replace(frame, players=(actor, other)), walking(intent, actor))
    assert result.social_cue == "yield_queue" and result.movement.direction == (0., 0., 0.)


def test_large_reorientation_has_one_short_pause_then_a_committed_departure():
    traffic = Traffic(Floor())
    actor, _, frame, intent = scene()
    frame = replace(frame, players=(actor,))
    traffic.adjust(frame, walking(intent, actor))
    backwards = walking(intent, actor, (-1., 0., 0.))
    turn = traffic.adjust(replace(frame, created_at=100.1), backwards)
    assert turn.movement.direction == (0., 0., 0.)
    resumed = traffic.adjust(replace(frame, created_at=100.4), backwards)
    assert resumed.movement.direction[0] < -.9


@pytest.mark.parametrize("kind", (MovementAffordance.JUMP, MovementAffordance.DROP,
                                  MovementAffordance.SWIM, MovementAffordance.BUILD_STEP))
def test_precise_and_airborne_movement_keeps_its_original_controller(kind):
    actor, _, frame, intent = scene()
    intent = replace(walking(intent, actor), movement=MovementIntent((1., 0., 0.), affordance=kind))
    assert Traffic(Floor()).adjust(frame, intent) == intent


def test_landing_settles_once_but_combat_can_interrupt_it():
    traffic = Traffic(Floor())
    actor, _, frame, intent = scene()
    frame = replace(frame, players=(replace(actor, grounded=False),))
    traffic.adjust(frame, walking(intent, actor))
    landed = replace(frame, players=(actor,), created_at=100.1)
    assert traffic.adjust(landed, walking(intent, actor)).movement.direction == (0., 0., 0.)
    urgent = replace(walking(intent, actor), priority=BotIntentPriority.COMBAT)
    assert traffic.adjust(landed, urgent).movement.direction[0] > .9
    assert traffic.adjust(replace(landed, created_at=100.4), walking(intent, actor)).movement.direction[0] > .9


def test_a_freshly_removed_yield_platform_is_revalidated_during_its_lease():
    world = Floor()
    traffic = Traffic(world)
    actor, human, frame, intent = scene()
    frame = replace(frame, players=(actor, replace(human, position=(11.5, 10.5, 37.75))))
    result = traffic.adjust(frame, walking(intent, actor))
    assert result.social_cue == "yield_human" and result.movement.direction[1] != 0
    world.bridge = True
    again = traffic.adjust(replace(frame, created_at=100.1), walking(intent, actor))
    assert again.movement.direction == (0., 0., 0.)


def test_final_destination_brakes_using_observed_speed_instead_of_overshooting():
    actor, _, frame, intent = scene(velocity=(.2, 0., 0.))
    frame = replace(frame, players=(actor,))
    intent = replace(walking(intent, actor), debug_navigation=(("route_remaining", 1),),
        movement=MovementIntent((1., 0., 0.), sprint=True,
            travel_source=actor.position, travel_waypoint=(11., 10.5, 37.75)))
    assert Traffic(Floor()).adjust(frame, intent).movement.direction == (0., 0., 0.)
