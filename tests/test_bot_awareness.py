"""Bots react to what a player in their place would be told, and to nothing else."""

from dataclasses import replace
import math
import random
import time
from types import SimpleNamespace

import pytest

import shared.constants as C
from server.bot_ai.awareness import Awareness, Reaction, notice_delay
from server.bot_ai.combat_tactics import exposed, find_shelter, firing_line
from server.bot_ai.messages import (
    BotAction, BotActionKind, BotIntentPriority, LookIntent, MovementAffordance,
    Stimulus, StimulusKind,
)
from server.bot_ai.messages import MovementIntent, ObjectiveSnapshot
from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
from server.bot_ai.stimuli import HEARING_DISTANCE, BotStimulusBus
from server.bot_ai.simple_navigation import RouteStep
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
    spot = awareness._lives[(1, 1)].shelter.spot
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
    dropping = replace(walking, movement=replace(walking.movement,
                                                 affordance=MovementAffordance.DROP))
    digging = replace(walking, action=BotAction(BotActionKind.MELEE, position=(1.0, 0.0, 22.0)))
    for busy in (jumping, aiming, dropping, digging):
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


# -- outnumbered or nearly dead ---------------------------------------------

def _foes(*ys, x=40.5):
    return tuple(_fighter(10 + index, TEAM2, x, y) for index, y in enumerate(ys))


def _fight(awareness, observer, now, foes, *, profile=None, decision=None, allies=()):
    """One decision of a fight in which the first listed foe is the target."""

    visible = foes[0] if foes else None
    return _react(awareness, observer, now, profile=profile, visible=visible,
                  decision=decision, others=(*foes, *allies))


def _pressed(observer, at, health=100):
    return replace(_hit(observer, at, attacker=10), health=health)


def test_alone_against_three_the_bot_breaks_sight_and_waits_out_of_it():
    world = _GridWorld(_pillar())
    awareness = _awake(world)
    foes = _foes(10.5, 6.5, 14.5)
    observer = _pressed(_fighter(1, TEAM1, 10.5, 10.5), 100.1, health=84)
    # Reading the fight takes a moment.
    assert _fight(awareness, observer, 100.2, foes) is None
    run = _fight(awareness, observer, 100.2 + notice_delay(_expert()) + 0.05, foes)
    assert run is not None and run.role == "disengage_cover" and run.heading[1] > 0.7
    spot = awareness._lives[(1, 1)].refuge.spot
    assert not exposed(world, spot, tuple(foe.eye for foe in foes))
    hidden = replace(observer, position=spot, eye=spot)
    hold = _react(awareness, hidden, 101.0, others=foes)
    assert hold.role == "disengage_hold" and hold.crouch and hold.look == foes[0].eye
    # Nobody near enough to join: it stays down instead of walking back out.
    life = awareness._lives[(1, 1)]
    assert _react(awareness, hidden, life.retreat_until + 0.5, others=foes).role == "disengage_hold"
    assert _react(awareness, hidden, life.retreat_wait + 1.5, others=foes) is None
    # The next fight is fought: no second retreat on the heels of the first.
    again = _pressed(hidden, life.retreat_wait + 1.6, health=60)
    assert _fight(awareness, again, life.retreat_wait + 2.5, foes) is None


def test_company_arriving_ends_the_wait_behind_cover():
    world = _GridWorld(_pillar())
    awareness = _awake(world)
    foes = _foes(10.5, 6.5, 14.5)
    observer = _pressed(_fighter(1, TEAM1, 10.5, 10.5), 100.1, health=84)
    _fight(awareness, observer, 100.2, foes)
    _fight(awareness, observer, 100.7, foes)
    life = awareness._lives[(1, 1)]
    hidden = replace(observer, position=life.refuge.spot, eye=life.refuge.spot)
    assert _react(awareness, hidden, 101.0, others=foes).role == "disengage_hold"
    mates = (_fighter(5, TEAM1, 8.5, 16.5), _fighter(6, TEAM1, 6.5, 14.5))
    assert _react(awareness, hidden, life.retreat_until + 0.2, others=(*foes, *mates)) is None
    assert life.retreat == ""


def test_an_enemy_coming_round_the_corner_is_fought_from_cover():
    world = _GridWorld(_pillar())
    awareness = _awake(world)
    foes = _foes(10.5, 6.5, 14.5)
    observer = _pressed(_fighter(1, TEAM1, 10.5, 10.5), 100.1, health=84)
    _fight(awareness, observer, 100.2, foes)
    _fight(awareness, observer, 100.7, foes)
    life = awareness._lives[(1, 1)]
    hidden = replace(observer, position=life.refuge.spot, eye=life.refuge.spot)
    assert _react(awareness, hidden, 101.0, others=foes).role == "disengage_hold"
    rusher = _fighter(10, TEAM2, hidden.position[0] + 6.0, hidden.position[1] + 4.0)
    assert _react(awareness, hidden, 101.5, visible=rusher, others=(rusher,)) is None


@pytest.mark.parametrize("case", ("not_hit", "duel", "behind_wall", "with_squad", "point_blank"))
def test_no_retreat_without_a_losing_fight_in_sight(case):
    world = _GridWorld(_pillar())
    awareness = _awake(world)
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    foes, allies = _foes(10.5, 6.5, 14.5), ()
    if case == "not_hit":
        pass  # three enemies in view, none of them shooting yet
    elif case == "duel":
        observer, foes = _pressed(observer, 100.1, health=84), _foes(10.5)
    elif case == "behind_wall":
        # Two of the three stand behind a wall: only the one in sight counts.
        world.cells |= _wall(30, range(0, 9), range(_FLOOR - 4, _FLOOR))
        world.cells |= _wall(30, range(13, 30), range(_FLOOR - 4, _FLOOR))
        observer = _pressed(observer, 100.1, health=84)
    elif case == "with_squad":
        observer = _pressed(observer, 100.1, health=84)
        allies = (_fighter(5, TEAM1, 8.5, 14.5), _fighter(6, TEAM1, 8.5, 6.5))
    else:
        observer, foes = _pressed(observer, 100.1, health=84), _foes(10.5, 9.5, 11.5, x=15.5)
    for step in range(16):
        assert _fight(awareness, observer, 100.2 + step * 0.125, foes, allies=allies) is None


def test_nearly_dead_ducks_behind_cover_but_does_not_run_across_the_open():
    foe = _foes(10.5)
    hurt = _pressed(_fighter(1, TEAM1, 10.5, 10.5), 100.1, health=18)
    sheltered = _awake(_GridWorld(_pillar()))
    _fight(sheltered, hurt, 100.2, foe)
    assert _fight(sheltered, hurt, 100.7, foe).role == "disengage_cover"
    open_ground = _awake(_GridWorld())
    for step in range(12):
        assert _fight(open_ground, hurt, 100.2 + step * 0.125, foe) is None


def test_a_fight_lost_fast_is_left_whatever_the_temperament():
    reckless = _expert(aggression=0.9, caution=0.2)
    foes = _foes(10.5, 14.5)
    awareness = _awake(_GridWorld(_pillar()), profile=reckless)
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    # Two guns are not enough to make this one leave ...
    steady = _pressed(observer, 100.1, health=92)
    for step in range(8):
        assert _fight(awareness, steady, 100.2 + step * 0.125, foes, profile=reckless) is None
    # ... until the health bar says how it will end.
    falling = _pressed(observer, 101.3, health=44)
    assert _fight(awareness, falling, 101.4, foes, profile=reckless) is None
    left = _fight(awareness, falling, 101.4 + notice_delay(reckless) + 0.05, foes, profile=reckless)
    assert left is not None and left.role == "disengage_cover"


def test_a_weak_bot_reads_the_odds_later():
    foes = _foes(10.5, 6.5, 14.5)
    observer = _pressed(_fighter(1, TEAM1, 10.5, 10.5), 100.1, health=84)
    slow, quick = _awake(_GridWorld(_pillar()), profile=_casual()), _awake(_GridWorld(_pillar()))
    for awareness, profile in ((slow, _casual()), (quick, _expert())):
        _fight(awareness, observer, 100.2, foes, profile=profile)
    at = 100.2 + notice_delay(_expert()) + 0.05
    assert _fight(quick, observer, at, foes).role == "disengage_cover"
    assert _fight(slow, observer, at, foes, profile=_casual()) is None
    late = 100.2 + notice_delay(_casual()) + 0.05
    assert _fight(slow, _pressed(observer, late - 0.3, health=68), late, foes,
                  profile=_casual()).role == "disengage_cover"


@pytest.mark.parametrize("role", ("carrier", "bodyguard", "defender_at_post", "raider_on_intel"))
def test_roles_that_must_hold_stay_in_a_losing_fight(role):
    awareness = _awake(_GridWorld(_pillar()))
    foes = _foes(10.5, 6.5, 14.5)
    observer = _pressed(_fighter(1, TEAM1, 10.5, 10.5), 100.1, health=40)
    decision = None
    if role == "carrier":
        observer = replace(observer, carried_entity_id=7)
    elif role == "bodyguard":
        decision = ModeBotDecision((60.0, 10.0, 0.0), "vip_guard_formation",
                                   posture=ModeBotPosture.ESCORT, objective_priority=0.95)
    elif role == "defender_at_post":
        decision = ModeBotDecision(observer.position, "territory_defend",
                                   posture=ModeBotPosture.DEFEND, objective_priority=0.95)
    else:
        decision = ModeBotDecision((14.0, 10.0, observer.position[2]), "ctf_attack_intel",
                                   objective_priority=0.9)
    for step in range(16):
        assert _fight(awareness, observer, 100.2 + step * 0.125, foes, decision=decision) is None
    # The same fight far from the objective is left.
    far = ModeBotDecision((90.0, 10.0, observer.position[2]), "ctf_attack_intel",
                          objective_priority=0.9)
    free = _awake(_GridWorld(_pillar()))
    raider = _pressed(_fighter(1, TEAM1, 10.5, 10.5), 100.1, health=40)
    _fight(free, raider, 100.2, foes, decision=far)
    assert _fight(free, raider, 100.7, foes, decision=far).role == "disengage_cover"


def test_open_ground_means_falling_back_toward_the_team():
    awareness = _awake(_GridWorld())
    foes = _foes(10.5, 6.5, 14.5)
    mate = _fighter(5, TEAM1, -30.5, 10.5)
    observer = _pressed(_fighter(1, TEAM1, 10.5, 10.5), 100.1, health=84)
    _fight(awareness, observer, 100.2, foes, allies=(mate,))
    back = _fight(awareness, observer, 100.7, foes, allies=(mate,))
    assert back.role == "fall_back" and back.goal == mate.position and back.sprint
    assert back.heading[0] < -0.9  # the raw stride away, for when no route is ready
    # Within reach of the teammate the retreat is over.
    home = replace(observer, position=(-26.5, 10.5, observer.position[2]))
    assert _react(awareness, home, 104.0, others=(*foes, mate)) is None


def test_worker_routes_a_fall_back_and_strides_away_while_no_route_exists():
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    reaction = Reaction("fall_back", goal=(-30.0, 10.5, observer.position[2]),
                        heading=(-1.0, 0.0, 0.0), sprint=True)
    stuck = SimpleBotBrain(_TacticalWorld(visible=False))
    state = _BotState(1, 1, observer.life_id)
    waiting = stuck._awareness_intent(_frame(observer), observer, state, reaction, 100.0)
    assert waiting.debug_role == "fall_back" and waiting.movement.direction == (-1.0, 0.0, 0.0)
    assert waiting.movement.sprint

    routed_world = _TacticalWorld(visible=False, route_step=RouteStep(
        (8.5, 10.5, observer.position[2]), MovementAffordance.WALK))
    routed = SimpleBotBrain(routed_world)
    walking = routed._awareness_intent(_frame(observer), observer,
                                       _BotState(1, 1, observer.life_id), reaction, 100.0)
    assert walking.debug_role.startswith("fall_back")
    assert walking.movement.travel_waypoint is not None and routed_world.plan_calls


# -- sounds -----------------------------------------------------------------

def _walker(player_id=7, team=TEAM2, position=(30.0, 10.0, 37.75), *, speed=5.6, alive=True,
            grounded=True, crouch=False, sneak=False, sprint=False):
    """A roster entry moving at ``speed`` blocks a second (velocity is per 1/32 s)."""

    return SimpleNamespace(
        id=player_id, team=team, position=position, alive=alive, spawned=True,
        grounded=grounded, vx=speed / 32.0, vy=0.0,
        input=SimpleNamespace(crouch=crouch, sneak=sneak, sprint=sprint))


def test_sounds_carry_no_farther_than_the_game_plays_them():
    bus = BotStimulusBus()
    assert HEARING_DISTANCE == float(C.HEARING_DISTANCE) == 50.0
    bus.publish(StimulusKind.SHOT, (0.0, 0.0, 0.0), source_id=3, team=TEAM2, radius=72.0, now=10.0)
    bus.publish(StimulusKind.EXPLOSION, (0.0, 0.0, 0.0), source_id=4, team=TEAM2, radius=80.0,
                now=10.0)
    near = bus.perceive((45.0, 0.0, 0.0), now=10.1, rng=random.Random(1))
    assert {event.kind for event in near} == {StimulusKind.SHOT, StimulusKind.EXPLOSION}
    assert bus.perceive((55.0, 0.0, 0.0), now=10.1, rng=random.Random(1)) == ()


@pytest.mark.parametrize("changes,steps", (
    ({}, 2),                      # a walk: one step per 0.512 s
    ({"sprint": True}, 3),        # a sprint: one per 0.386 s
    ({"crouch": True}, 0),
    ({"sneak": True}, 0),
    ({"grounded": False}, 0),
    ({"speed": 0.4}, 0),
    ({"alive": False}, 0),
))
def test_footsteps_follow_the_retail_rule(changes, steps):
    bus = BotStimulusBus()
    walker = _walker(**changes)
    # One second of perception refreshes at 10 Hz.
    published = sum(bus.note_footsteps((walker,), 20.0 + tick * 0.1) for tick in range(10))
    assert published == steps
    heard = bus.perceive((20.0, 10.0, 37.75), now=20.95, rng=random.Random(2))
    assert all(event.kind is StimulusKind.FOOTSTEP and event.source_id == 7 for event in heard)
    assert bool(heard) == bool(steps)


def test_a_listener_is_spared_its_own_noise_and_its_squads_steps():
    bus = BotStimulusBus()
    bus.note_footsteps((_walker(1, TEAM1, (10.0, 10.0, 37.75)),      # the listener
                        _walker(2, TEAM1, (12.0, 10.0, 37.75)),      # a teammate
                        _walker(7, TEAM2, (20.0, 10.0, 37.75))), 30.0)
    bus.publish(StimulusKind.SHOT, (12.0, 10.0, 37.0), source_id=2, team=TEAM1, now=30.0)
    bus.publish(StimulusKind.SHOT, (10.0, 10.0, 37.0), source_id=1, team=TEAM1, now=30.0)
    heard = bus.perceive((10.0, 10.0, 37.75), now=30.1, rng=random.Random(3),
                         observer_id=1, team=TEAM1)
    assert sorted((event.kind.value, event.source_id) for event in heard) == [
        ("footstep", 7), ("shot", 2)]
    # Without a listener identity nothing is filtered (older callers).
    assert len(bus.perceive((10.0, 10.0, 37.75), now=30.1, rng=random.Random(3))) == 5
    # A step is gone long before the shot that was published with it.
    late = bus.perceive((10.0, 10.0, 37.75), now=30.9, rng=random.Random(3))
    assert {event.kind for event in late} == {StimulusKind.SHOT}


def test_digging_and_building_are_heard_only_when_someone_made_them():
    from server.audio import SND_DIG_HIT_BLOCK, play_sound

    sent = []
    server = SimpleNamespace(bot_stimuli=BotStimulusBus(),
                             broadcast=lambda data, **kwargs: sent.append(data))
    digger = SimpleNamespace(id=7, team=TEAM2)
    play_sound(server, SND_DIG_HIT_BLOCK, position=(20, 10, 40), reliable=False, source=digger)
    play_sound(server, SND_DIG_HIT_BLOCK, position=(60, 10, 40), reliable=False)  # no actor
    play_sound(server, SND_DIG_HIT_BLOCK, source=digger)                          # no place
    assert len(sent) == 3
    heard = server.bot_stimuli.perceive((10.0, 10.0, 40.0), now=time.monotonic() + 0.1,
                                        rng=random.Random(4), limit=8)
    assert [(event.kind, event.source_id, event.team) for event in heard] == [
        (StimulusKind.BLOCK_DESTROYED, 7, TEAM2)]


def _sound(kind, position, at, *, source=7, team=TEAM2):
    return Stimulus(kind=kind, position=position, created_at=at, expires_at=at + 1.25,
                    source_id=source, team=team, uncertainty=1.5)


def _hear(awareness, observer, now, *sounds, profile=None, visible=None, decision=None,
          others=()):
    profile = profile or _expert()
    frame = replace(_frame(observer, *others, created_at=now), profile=profile, stimuli=sounds)
    return awareness.react(frame, observer, profile, visible, decision, now)


def _look(awareness, observer, now, intent=None):
    intent = intent or replace(travel_intent(), bot_id=1, bot_generation=1)
    return awareness.overlay(_frame(observer, created_at=now), intent).look


def test_an_unseen_shot_turns_the_head_and_nothing_else():
    awareness = _awake(_GridWorld())
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    behind = (-14.5, 12.5, observer.position[2])
    assert _hear(awareness, observer, 100.1, _sound(StimulusKind.SHOT, behind, 100.05)) is None
    # Not before it has sunk in.
    assert not _look(awareness, observer, 100.12).glance
    look = _look(awareness, observer, 100.1 + 0.5 * notice_delay(_expert()) + 0.05)
    assert look.glance and look.target[:2] == behind[:2] and look.target[2] == observer.eye[2]
    life = awareness._lives[(1, 1)]
    assert life.alert > 0.3
    # The glance is short for a bot on the move ...
    assert not _look(awareness, observer, life.glance_until + 0.05).glance
    # ... and longer for one standing its ground.
    standing = replace(travel_intent(), bot_id=1, bot_generation=1, movement=MovementIntent())
    assert _look(awareness, observer, life.glance_until + 0.05, standing).glance
    assert not _look(awareness, observer, life.watch_until + 0.05, standing).glance


@pytest.mark.parametrize("case", ("silence", "friendly", "own", "in_view", "old_news"))
def test_no_sound_no_glance(case):
    awareness = _awake(_GridWorld())
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    sounds, others = (), ()
    if case == "friendly":
        sounds = (_sound(StimulusKind.SHOT, (-14.5, 12.5, 37.75), 100.05, source=5, team=TEAM1),)
    elif case == "own":
        sounds = (_sound(StimulusKind.BLOCK_DESTROYED, (11.5, 10.5, 40.0), 100.05, source=1,
                         team=TEAM1),)
    elif case == "in_view":
        # The bot is looking straight at the enemy who fired.
        others = (_fighter(7, TEAM2, 40.5, 10.5),)
        sounds = (_sound(StimulusKind.SHOT, (40.5, 10.5, 37.75), 100.05),)
    elif case == "old_news":
        _hear(awareness, observer, 99.0, _sound(StimulusKind.SHOT, (-14.5, 12.5, 37.75), 98.9))
        awareness._lives[(1, 1)].glance = None
        sounds = (_sound(StimulusKind.SHOT, (-14.5, 12.5, 37.75), 98.9),)  # the same event again
    for step in range(12):
        now = 100.1 + step * 0.125
        assert _hear(awareness, observer, now, *sounds, others=others) is None
        assert not _look(awareness, observer, now).glance


def test_what_is_heard_depends_on_the_sound_the_distance_and_the_listener():
    observer = _fighter(1, TEAM1, 10.5, 10.5)

    def notices(kind, distance, profile):
        awareness = _awake(_GridWorld(), profile=profile)
        _hear(awareness, observer, 100.1, _sound(
            kind, (10.5 - distance, 10.5, observer.position[2]), 100.05), profile=profile)
        return awareness._lives[(1, 1)].glance is not None

    assert notices(StimulusKind.SHOT, 40.0, _expert())
    assert not notices(StimulusKind.SHOT, 40.0, _casual())
    assert notices(StimulusKind.SHOT, 25.0, _casual())
    assert notices(StimulusKind.FOOTSTEP, 12.0, _casual())
    assert notices(StimulusKind.FOOTSTEP, 28.0, _expert())
    assert not notices(StimulusKind.FOOTSTEP, 28.0, _casual())
    assert not notices(StimulusKind.FOOTSTEP, 45.0, _expert())
    assert notices(StimulusKind.BLOCK_DESTROYED, 20.0, _expert())


def test_a_fight_in_hand_and_a_place_already_checked_are_not_looked_at_again():
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    behind = (-14.5, 12.5, observer.position[2])
    enemy = _fighter(9, TEAM2, 40.5, 10.5)
    fighting = _awake(_GridWorld())
    _hear(fighting, observer, 100.1, _sound(StimulusKind.SHOT, behind, 100.05),
          visible=enemy, others=(enemy,))
    assert fighting._lives[(1, 1)].glance is None
    # Loud enough to wind the bot up even so.
    assert fighting._lives[(1, 1)].alert > 0.0

    awareness = _awake(_GridWorld())
    _hear(awareness, observer, 100.1, _sound(StimulusKind.SHOT, behind, 100.05))
    life = awareness._lives[(1, 1)]
    first = life.glance_from
    # More shots from the same place: already looked there.
    _hear(awareness, observer, life.glance_ready_at + 0.1, _sound(
        StimulusKind.SHOT, (behind[0] + 3.0, behind[1], behind[2]), life.glance_ready_at))
    assert life.glance_from == first
    # The noise has come much closer: that is news.
    closer = (0.5, 11.5, observer.position[2])
    _hear(awareness, observer, life.glance_ready_at + 0.5, _sound(
        StimulusKind.FOOTSTEP, closer, life.glance_ready_at + 0.4))
    assert life.glance_from > first and life.glance[:2] == closer[:2]


def test_objective_carriers_do_not_look_round_for_noises():
    awareness = _awake(_GridWorld())
    carrier = replace(_fighter(1, TEAM1, 10.5, 10.5), carried_entity_id=7)
    _hear(awareness, carrier, 100.1, _sound(StimulusKind.SHOT, (-14.5, 12.5, 37.75), 100.05))
    assert awareness._lives[(1, 1)].glance is None


def test_a_bot_that_has_been_hearing_gunfire_understands_a_hit_sooner():
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    calm, tense = _awake(_GridWorld(_pillar())), _awake(_GridWorld(_pillar()))
    _hear(tense, observer, 100.0, _sound(StimulusKind.EXPLOSION, (4.5, 10.5, 37.75), 99.95))
    hit = _hit(observer, 100.1)
    at = 100.1 + 0.7 * notice_delay(_expert())
    assert _react(calm, hit, at) is None
    assert _react(tense, hit, at) is not None


def test_director_publishes_footsteps_and_filters_what_each_bot_hears():
    server, director, bot, runtime = _facing_fixture()
    frames = []
    director.supervisor = SimpleNamespace(submit_frame=frames.append)
    server.bot_stimuli = BotStimulusBus()
    enemy = _walker(77, TEAM2, (bot.x + 12.0, bot.y, bot.z))
    server.players[77] = enemy
    bot.vx, bot.grounded = 5.6 / 32.0, True  # a walk, in the server's velocity units
    now = time.monotonic()
    try:
        director._perception_cache_until = 0.0
        director._perception_build_players = (bot, enemy)
        director._perception_build_snapshots = []
        director._perception_build_index = 0
        director._snapshot_player = lambda player: SimpleNamespace(
            player_id=int(player.id), position=tuple(player.position))
        runtime.next_perception_at = 0.0
        assert director._publish_due_perception(now)       # snapshots, and the footsteps
        assert not director._publish_due_perception(now)   # the observer frame
    finally:
        server.players.pop(77, None)
    assert {event.source_id for event in server.bot_stimuli._events} == {int(bot.id), 77}
    assert [(event.kind, event.source_id) for event in frames[-1].stimuli] == [
        (StimulusKind.FOOTSTEP, 77)]


# -- a teammate killed beside the bot ---------------------------------------

_SNIPER = (-25.5, 10.5, _FLOOR - 2.25)


def _death(position, at, *, killer=7, team=TEAM1):
    return Stimulus(kind=StimulusKind.DEATH, position=position, created_at=at,
                    expires_at=at + 1.5, source_id=killer, team=team, uncertainty=0.75)


def _kill_beside(awareness, observer, now, *, profile=None, decision=None, visible=None,
                 shot=True, death=None, others=(), objectives=()):
    """One frame in which a teammate two blocks away is shot dead."""

    death = death or _death((10.5, 12.5, observer.position[2]), now - 0.05)
    sounds = ((_sound(StimulusKind.SHOT, _SNIPER, now - 0.06),) if shot else ()) + (death,)
    profile = profile or _expert()
    frame = replace(_frame(observer, *others, created_at=now, objectives=objectives),
                    profile=profile, stimuli=sounds)
    return awareness.react(frame, observer, profile, visible, decision, now)


def test_director_publishes_a_death_with_its_killer_for_bots_in_earshot():
    server, director, bot, _runtime = _facing_fixture()
    server.bot_stimuli = BotStimulusBus()
    killer = SimpleNamespace(id=42, team=TEAM2, name="Sniper", position=(0.0, 0.0, 0.0))
    director.on_player_killed(bot, killer, int(C.HEADSHOT_KILL))
    heard = server.bot_stimuli.perceive(tuple(bot.position), now=time.monotonic() + 0.1,
                                        rng=random.Random(5))
    assert [(event.kind, event.source_id, event.team) for event in heard] == [
        (StimulusKind.DEATH, 42, int(bot.team))]
    far = (bot.x + HEARING_DISTANCE + 5.0, bot.y, bot.z)
    assert server.bot_stimuli.perceive(far, now=time.monotonic() + 0.1,
                                       rng=random.Random(5)) == ()


def test_a_teammate_shot_dead_beside_the_bot_turns_it_to_the_shot_and_into_cover():
    world = _GridWorld(_wall(8, range(12, 18), range(_FLOOR - 4, _FLOOR))
                       | _wall(7, range(12, 18), range(_FLOOR - 4, _FLOOR)))
    awareness = _awake(world)
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    assert _kill_beside(awareness, observer, 100.1) is None  # not inside its reaction time
    life = awareness._lives[(1, 1)]
    assert life.alert >= 0.6
    look = _look(awareness, observer, life.glance_from + 0.05)
    assert look.glance and look.target[:2] == _SNIPER[:2]
    run = _react(awareness, observer, life.fire_noticed_at + 0.05)
    assert run is not None and run.role == "ally_down_cover"
    spot = life.shelter.spot
    assert not world.has_line_of_sight(spot, _SNIPER)
    hold = _react(awareness, replace(observer, position=spot, eye=spot),
                  life.fire_noticed_at + 0.4)
    assert hold.role == "ally_down_hold" and hold.look == _SNIPER and hold.crouch
    # A hit while it lies there makes this an ordinary fight under fire.
    hit = _hit(replace(observer, position=spot, eye=spot), life.fire_noticed_at + 0.5,
               source=_SNIPER, attacker=7)
    assert _react(awareness, hit, life.fire_noticed_at + 0.6).role.startswith("under_fire")


@pytest.mark.parametrize("case", ("far", "out_of_sight", "enemy_died", "accident", "none"))
def test_only_a_teammate_killed_by_an_enemy_within_sight_is_the_bots_business(case):
    world = _GridWorld()
    awareness = _awake(world)
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    height, others, death = observer.position[2], (), None
    if case == "far":
        death = _death((10.5, 40.5, height), 100.05)
    elif case == "out_of_sight":
        world.cells |= _wall(10, range(18, 22), range(_FLOOR - 4, _FLOOR))
        death = _death((10.5, 24.5, height), 100.05)
    elif case == "enemy_died":
        death = _death((10.5, 12.5, height), 100.05, killer=5, team=TEAM2)
    elif case == "accident":
        # Killed by a teammate's stray rocket: nobody to hide from.
        death = _death((10.5, 12.5, height), 100.05, killer=5)
        others = (_fighter(5, TEAM1, 20.5, 10.5),)
    for step in range(16):
        now = 100.1 + step * 0.125
        if case == "none":
            assert _react(awareness, observer, now) is None
        else:
            assert _kill_beside(awareness, observer, now, death=death, others=others,
                                shot=False) is None
    life = awareness._lives[(1, 1)]
    assert life.fire_from is None and life.glance is None and life.alert == 0.0


def test_a_killer_too_far_to_hear_leaves_the_enemy_side_of_the_map():
    awareness = _awake(_GridWorld())
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    anchors = (ObjectiveSnapshot("team_anchor", TEAM2, (200.5, 10.5, observer.position[2])),)
    _kill_beside(awareness, observer, 100.1, shot=False, objectives=anchors)
    life = awareness._lives[(1, 1)]
    assert life.glance is not None and life.glance[0] > 40.0  # toward the enemy base
    # Without even that, there is nothing to turn to.
    blind = _awake(_GridWorld())
    _kill_beside(blind, observer, 100.1, shot=False)
    assert blind._lives[(1, 1)].glance is None and blind._lives[(1, 1)].fire_from is None


def test_temperament_and_the_objective_decide_between_a_look_and_cover():
    observer = _fighter(1, TEAM1, 10.5, 10.5)
    bold = _awake(_GridWorld(), profile=_casual(caution=0.3))
    _kill_beside(bold, observer, 100.1, profile=_casual(caution=0.3))
    assert bold._lives[(1, 1)].glance is not None and bold._lives[(1, 1)].fire_from is None

    raid = ModeBotDecision((90.0, 10.0, 0.0), "ctf_attack_intel", objective_priority=0.9)
    raider = _awake(_GridWorld())
    _kill_beside(raider, observer, 100.1, decision=raid)
    assert raider._lives[(1, 1)].glance is not None and raider._lives[(1, 1)].fire_from is None

    carrier = _awake(_GridWorld())
    _kill_beside(carrier, replace(observer, carried_entity_id=7), 100.1)
    assert carrier._lives[(1, 1)].glance is None

    # A bot already shooting at someone keeps shooting.
    enemy = _fighter(9, TEAM2, 40.5, 10.5)
    busy = _awake(_GridWorld())
    _kill_beside(busy, observer, 100.1, visible=enemy, others=(enemy,))
    assert busy._lives[(1, 1)].glance is None and busy._lives[(1, 1)].alert >= 0.6
