"""Bots react to what a player in their place would be told, and to nothing else."""

from dataclasses import replace
import math

import pytest

import shared.constants as C
from server.bot_ai.awareness import Awareness, Reaction, notice_delay
from server.bot_ai.combat_tactics import exposed, find_shelter, firing_line
from server.bot_ai.messages import BotIntentPriority, LookIntent, MovementAffordance, MovementIntent
from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
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
