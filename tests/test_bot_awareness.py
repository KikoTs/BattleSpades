"""Bots react to what a player in their place would be told, and to nothing else."""

from dataclasses import replace
import math

import pytest

import shared.constants as C
from server.bot_ai.awareness import Awareness, Reaction, notice_delay
from server.bot_ai.combat_tactics import exposed, find_shelter, firing_line
from server.bot_ai.messages import BotIntentPriority, LookIntent, MovementAffordance, MovementIntent
from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState
from server.game_constants import TEAM1, TEAM2
from tests.test_bot_combat_tactics import _FLOOR, _GridWorld, _fighter, _wall
from tests.test_bot_live_travel_motor import travel_intent
from tests.test_bot_architecture import _facing_fixture
from tests.test_simple_bot_tactics import _TacticalWorld, _frame, _profile

_SHOOTER = (40.5, 10.5, _FLOOR - 2.25)


def _hit(observer, at, source=_SHOOTER, attacker=2, kind=int(C.WEAPON_KILL)):
    return replace(observer, last_damage_at=at, last_damage_source_id=attacker,
                   last_damage_source_position=source, last_damage_kind=kind)


def _expert(**changes):
    return replace(_profile(), **{"skill": 0.9, "reaction_time": 0.2, "caution": 0.5,
                                  "aggression": 0.5, **changes})


def _casual(**changes):
    return replace(_profile(), **{"skill": 0.25, "reaction_time": 0.6, "caution": 0.4,
                                  "aggression": 0.5, **changes})


def _awake(world, observer=None, **kwargs):
    """An awareness that has already seen this life in a quiet moment."""

    awareness = Awareness(world)
    _react(awareness, observer or _fighter(1, TEAM1, 10.5, 10.5), 100.0, **kwargs)
    return awareness


def _react(awareness, observer, now, *, profile=None, visible=None, decision=None, others=()):
    profile = profile or _expert()
    frame = replace(_frame(observer, *others, created_at=now), profile=profile)
    return awareness.react(frame, observer, profile, visible, decision, now)


def _pillar():
    """A thick wall a few strides to the bot's side, toward the shooter."""

    return (_wall(12, range(12, 18), range(_FLOOR - 4, _FLOOR))
            | _wall(13, range(12, 18), range(_FLOOR - 4, _FLOOR)))


def test_shelter_hides_from_the_whole_firing_line_and_is_a_real_walk():
    world = _GridWorld(_pillar())
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    line = firing_line(observer.position, _SHOOTER)
    assert len(line) == 3 and exposed(world, observer.eye, line)
    spot = find_shelter(world, observer, line)
    assert spot is not None and not exposed(world, spot, line)
    assert spot[0] <= observer.position[0] + 0.5  # never a step toward the shooter
    assert find_shelter(_GridWorld(), observer, line) is None


def test_no_stimulus_no_reaction():
    awareness = Awareness(_GridWorld(_pillar()))
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    for step in range(40):
        assert _react(awareness, observer, 100.0 + step * 0.125) is None
    intent = travel_intent()
    frame = _frame(observer, created_at=105.0)
    assert awareness.overlay(frame, replace(intent, bot_id=1, bot_generation=1)).look == intent.look


def test_hit_from_an_unseen_shooter_turns_the_bot_and_sends_it_to_cover():
    world = _GridWorld(_pillar())
    awareness = Awareness(world)
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    assert _react(awareness, observer, 100.0) is None
    hit = _hit(observer, 100.1)
    # Nobody reacts inside their own reaction time.
    assert _react(awareness, hit, 100.15) is None
    first = _react(awareness, hit, 100.1 + notice_delay(_expert()) + 0.05)
    assert first is not None and first.role == "under_fire_cover"
    assert first.look == _SHOOTER  # the first beat is a look at the shot
    assert first.heading[1] > 0.7  # and the feet already head behind the wall
    later = _react(awareness, hit, 101.0)
    assert later.role == "under_fire_cover" and later.look != _SHOOTER
    assert later.priority is BotIntentPriority.COMBAT


def test_arrival_behind_cover_holds_facing_the_shot_and_a_new_hit_moves_on():
    world = _GridWorld(_pillar())
    awareness = _awake(world)
    observer = _hit(_fighter(1, TEAM1, 10.5, 10.5), 100.1)
    run = _react(awareness, observer, 100.6)
    spot = awareness._lives[(1, 1)].shelter
    assert run.role == "under_fire_cover" and spot is not None
    arrived = replace(observer, position=spot, eye=spot)
    hold = _react(awareness, arrived, 101.0)
    assert hold.role == "under_fire_hold" and hold.crouch and hold.look == _SHOOTER
    assert hold.heading == (0.0, 0.0, 0.0)
    again = _react(awareness, _hit(arrived, 101.4), 101.5)
    assert again.role != "under_fire_hold"  # that spot was not cover after all


def test_open_ground_means_a_zigzag_away_from_the_shot():
    awareness = _awake(_GridWorld())
    observer = _hit(_fighter(1, TEAM1, 10.5, 10.5), 100.1)
    first = _react(awareness, observer, 100.6)
    assert first.role == "under_fire_evade" and first.sprint
    assert first.heading[0] < 0.0  # away from the shooter at +x
    life = awareness._lives[(1, 1)]
    second = _react(awareness, observer, life.dash_until + 0.01)
    assert second.role == "under_fire_evade"
    assert second.heading[1] * first.heading[1] < 0.0  # the other side this time
    assert second.look == _SHOOTER  # each new leg starts with a look back


def test_the_reaction_ends_and_the_bold_go_looking():
    awareness = _awake(_GridWorld())
    observer = _hit(_fighter(1, TEAM1, 10.5, 10.5), 100.1)
    assert _react(awareness, observer, 100.6, profile=_expert(aggression=0.8)) is not None
    done = _react(awareness, observer, 110.0, profile=_expert(aggression=0.8))
    assert done == Reaction(suspect=_SHOOTER)
    assert _react(awareness, observer, 110.2, profile=_expert(aggression=0.8)) is None

    timid = _awake(_GridWorld())
    shy = _casual(aggression=0.3)
    _react(timid, observer, 100.6, profile=shy)
    _react(timid, _hit(observer, 102.0), 104.0, profile=shy)
    assert timid._lives[(1, 1)].fire_from is not None
    assert _react(timid, observer, 110.0, profile=shy) is None
    assert timid._lives[(1, 1)].fire_from is None


@pytest.mark.parametrize("case", ("burn", "self", "teammate", "stale", "previous_life"))
def test_damage_that_names_no_enemy_direction_is_not_a_shot(case):
    awareness = Awareness(_GridWorld(_pillar()))
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    others = ()
    if case == "previous_life":
        # The hit that killed the last life is still in the snapshot.
        observer = _hit(observer, 99.5)
        hit, now = observer, 100.6
    else:
        assert _react(awareness, observer, 100.0) is None
        now = 100.6
        if case == "burn":
            hit = _hit(observer, 100.1, kind=int(C.BLOCKFIRE_KILL))
        elif case == "self":
            hit = _hit(observer, 100.1, attacker=1)
        elif case == "teammate":
            hit = _hit(observer, 100.1, attacker=5)
            others = (_fighter(5, TEAM1, 40.5, 10.5),)
        else:
            hit, now = _hit(observer, 100.1), 103.0
    for step in range(12):
        assert _react(awareness, hit, now + step * 0.125, others=others) is None


def test_a_shooter_in_sight_is_left_to_ordinary_combat():
    awareness = _awake(_GridWorld(_pillar()))
    observer = _hit(_fighter(1, TEAM1, 10.5, 10.5), 100.1)
    enemy = _fighter(2, TEAM2, 40.5, 10.5)
    assert _react(awareness, observer, 100.6, visible=enemy, others=(enemy,)) is None


def test_a_weak_bot_reacts_later_and_first_only_looks():
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    assert notice_delay(_casual()) > 3.0 * notice_delay(_expert())

    weak = Awareness(_GridWorld(_pillar()))
    _react(weak, observer, 100.0, profile=_casual())
    hit = _hit(observer, 100.1)
    # An expert is already running at this point.
    assert _react(weak, hit, 100.6, profile=_casual()) is None
    noticed = 100.1 + notice_delay(_casual()) + 0.05
    assert _react(weak, hit, noticed, profile=_casual()) is None
    intent = replace(travel_intent(), bot_id=1, bot_generation=1)
    glanced = weak.overlay(_frame(hit, created_at=noticed), intent)
    assert glanced.look == LookIntent(_SHOOTER, glance=True)
    assert glanced.movement == intent.movement  # the feet keep their route
    second = _react(weak, _hit(hit, noticed + 0.1), noticed + 0.2, profile=_casual())
    assert second is not None and second.role.startswith("under_fire")


def test_a_glance_never_takes_the_eyes_off_exact_work():
    awareness = _awake(_GridWorld())
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    _react(awareness, _hit(observer, 100.1), 100.2, profile=_casual())
    assert _react(awareness, _hit(observer, 100.1), 102.0, profile=_casual()) is None
    frame = _frame(observer, created_at=102.1)
    walking = replace(travel_intent(), bot_id=1, bot_generation=1)
    assert awareness.overlay(frame, walking).look.glance
    jumping = replace(walking, movement=replace(walking.movement, jump=True,
                                                affordance=MovementAffordance.JUMP))
    aiming = replace(walking, look=LookIntent((20.0, 0.0, 20.0), visible=True))
    ledge = replace(walking, movement=replace(walking.movement, walk_drop=4))
    for busy in (jumping, aiming, ledge):
        assert awareness.overlay(frame, busy) == busy
    # The glance itself is short.
    assert awareness.overlay(_frame(observer, created_at=104.0), walking) == walking


def test_objective_carriers_and_fleeing_roles_keep_running():
    world = _GridWorld(_pillar())
    hit = _hit(_fighter(1, TEAM1, 10.5, 10.5), 100.1)
    carrier = _awake(world)
    assert _react(carrier, replace(hit, carried_entity_id=7), 100.6) is None
    assert _react(carrier, replace(hit, can_shoot=False), 100.8) is None
    fleeing = _awake(world)
    retreat = ModeBotDecision((0.0, 0.0, 0.0), "vip_retreat", posture=ModeBotPosture.EVASIVE,
                              objective_priority=1.0)
    assert _react(fleeing, hit, 100.6, decision=retreat) is None
    attacker = _awake(world)
    raid = ModeBotDecision((90.0, 10.0, 0.0), "ctf_attack_intel", objective_priority=0.9)
    assert _react(attacker, hit, 100.6, decision=raid).role == "under_fire_cover"


def test_zombies_do_not_take_cover():
    zombie = replace(_fighter(1, TEAM1, 10.5, 10.5), class_id=int(C.CLASS_ZOMBIE))
    awareness = _awake(_GridWorld(_pillar()), zombie)
    assert _react(awareness, _hit(zombie, 100.1), 100.6) is None


def test_worker_takes_cover_when_hit_and_walks_on_when_not():
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    quiet = SimpleBotBrain(_TacticalWorld(visible=False))
    calm = quiet.decide(replace(_frame(observer), profile=_expert()))
    assert calm is None or not calm.debug_role.startswith("under_fire")

    brain = SimpleBotBrain(_TacticalWorld(visible=False))
    brain.decide(replace(_frame(observer, created_at=100.0), profile=_expert()))
    hit = _hit(observer, 100.1)
    intent = brain.decide(replace(_frame(hit, created_at=100.6), frame_id=2, profile=_expert()))
    assert intent.debug_role.startswith("under_fire")
    assert intent.priority is BotIntentPriority.COMBAT
    assert intent.look is not None and not intent.look.visible


def test_worker_hands_the_shot_origin_to_the_last_seen_chase():
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    brain = SimpleBotBrain(_TacticalWorld(visible=False))
    state = _BotState(1, 1, observer.life_id)
    frame = _frame(observer, created_at=100.0)
    assert brain._awareness_intent(frame, observer, state, Reaction(suspect=_SHOOTER), 100.0) is None
    assert state.contact_position == _SHOOTER and state.contact_until > 100.0


def test_motor_turns_the_head_for_a_glance_and_keeps_the_route_under_the_feet():
    _, director, bot, runtime = _facing_fixture()
    runtime.profile = replace(runtime.profile, aim_noise=0.0)
    eye = tuple(float(value) for value in bot.eye)
    behind = (eye[0] - 20.0, eye[1] + 1.0, eye[2])
    walking = travel_intent(source=tuple(bot.position),
                            waypoint=(bot.x + 6.0, bot.y, bot.z))
    runtime.intent = replace(walking, look=LookIntent(behind, glance=True))
    for _ in range(12):
        director._apply_motor(runtime, 100.0, 0.05)
    assert math.cos(runtime.motor.yaw) < -0.8  # looking back
    # Keys are relative to the view: going on along +x is now "backward".
    assert runtime.movement_input[1] and not runtime.movement_input[0]

    runtime.motor.yaw = 0.0
    runtime.intent = replace(walking, look=LookIntent(behind))
    for _ in range(12):
        director._apply_motor(runtime, 100.0, 0.05)
    assert math.cos(runtime.motor.yaw) > 0.8  # an ordinary route look is ignored
