"""Mode objective roles: every live bot has an order and teams split the work."""

from __future__ import annotations

from dataclasses import replace
import itertools
import math

import pytest
import shared.constants as C

from server.bot_ai.messages import ObjectiveSnapshot, PerceptionFrame, PlayerSnapshot
from server.bot_ai.policies import (
    _MODE_STRATEGIES,
    _POLICIES,
    ModePolicyMemory,
    objective_decision_for,
)
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain


BLUE, GREEN = 2, 3
NOW = 1000.0
BLUE_HOME = (60.5, 256.5, 40.0)
GREEN_HOME = (450.5, 256.5, 40.0)


def _player(player_id, team, position=BLUE_HOME, *, carried=-1, health=100,
            class_id=0, is_bot=True, loadout=(2, 5, 6), blocks=50, **extra):
    return PlayerSnapshot(
        player_id=player_id, generation=1, team=team, class_id=class_id,
        alive=True, spawned=True, position=position, eye=position,
        orientation=(1.0, 0.0, 0.0), velocity=(0.0, 0.0, 0.0), health=health,
        tool=6, blocks=blocks, ammo_clip=10, ammo_reserve=30, is_bot=is_bot,
        loadout=loadout, carried_entity_id=carried, life_id=1, **extra,
    )


def _frame(mode, observer, *players, objectives=(), phase="active", now=NOW):
    return PerceptionFrame(
        frame_id=1, map_epoch=1, mode_epoch=1, topology_version=0,
        observer_id=observer.player_id, observer_generation=1,
        created_at=now, mode_id=mode,
        players=(observer, *players), objectives=tuple(objectives), mode_phase=phase,
    )


def _anchors():
    return (ObjectiveSnapshot("team_anchor", BLUE, BLUE_HOME),
            ObjectiveSnapshot("team_anchor", GREEN, GREEN_HOME))


def _ctf_objectives(own=(-1, 0, None), enemy=(-1, 0, None)):
    """Blue's view: ``(carrier id, state, position)`` for each intel."""

    return (
        ObjectiveSnapshot("ctf_base", BLUE, BLUE_HOME),
        ObjectiveSnapshot("ctf_base", GREEN, GREEN_HOME),
        ObjectiveSnapshot("ctf_intel", BLUE, own[2] or BLUE_HOME,
                          carrier_id=own[0], state=own[1]),
        ObjectiveSnapshot("ctf_intel", GREEN, enemy[2] or GREEN_HOME,
                          carrier_id=enemy[0], state=enemy[1]),
        *_anchors(),
    )


# ------------------------------------------------------------ never idle (F01)

def test_classic_bot_goes_for_the_enemy_intel_its_carrier_dropped_out_of_sight():
    # Crossroads, Classic CTF: the Blue carrier died 300 blocks from this bot.
    # Nothing returns a Classic intel, so the bot stood at its spawn for the
    # remaining four minutes.
    observer = _player(5, BLUE, (64.5, 348.5, 40.0))
    drop = (362.0, 183.0, 40.0)
    decision = objective_decision_for(
        _frame("cctf", observer, objectives=_ctf_objectives(enemy=(-1, 1, drop))), observer)
    assert decision.role == "classic_ctf_attack_intel"
    assert decision.position == drop


def test_worker_walks_toward_a_far_classic_drop_instead_of_idling():
    class _Ground:
        def get_solid(self, _x, _y, z):
            return z >= 100

    world = SimpleVoxelWorld()
    world._vxl = _Ground()
    world.map_epoch = world.topology_version = 1
    brain = SimpleBotBrain(world)
    observer = _player(1, BLUE, (30.5, 20.5, 97.75), weapon_tool=int(C.RIFLE_TOOL),
                       loadout=(int(C.RIFLE_TOOL), int(C.SPADE_TOOL)))
    drop = (230.5, 20.5, 97.75)
    frame = replace(
        _frame("cctf", observer, objectives=(
            ObjectiveSnapshot("ctf_base", BLUE, (10.5, 20.5, 97.75)),
            ObjectiveSnapshot("ctf_intel", BLUE, (13.5, 20.5, 97.75)),
            ObjectiveSnapshot("ctf_intel", GREEN, drop, state=1),
        )),
        behavior_version="cooperative",
    )
    intent = brain.decide(frame)
    assert intent is not None
    assert intent.debug_role != "idle_no_goal"
    assert intent.movement.direction[0] > 0.9


_INTEL_STATES = ("home", "dropped_near", "dropped_far", "carried")


def _intel_state(name, *, own, holder):
    if name == "home":
        return (-1, 0, None)
    if name == "dropped_near":
        return (-1, 1, (120.0, 256.0, 40.0))
    if name == "dropped_far":
        return (-1, 1, (430.0, 80.0, 40.0) if own else (40.0, 470.0, 40.0))
    return (holder, 2, (250.0, 250.0, 40.0))


@pytest.mark.parametrize("mode", ["ctf", "cctf"])
@pytest.mark.parametrize("own_state,enemy_state",
                         list(itertools.product(_INTEL_STATES, _INTEL_STATES)))
def test_ctf_policy_gives_every_bot_an_order_in_every_intel_state(mode, own_state, enemy_state):
    # Seven Blue bots spread from home to the enemy base, one enemy carrier id.
    team = [_player(index, BLUE, (60.5 + 62.0 * index, 250.0 + 9.0 * index, 40.0))
            for index in range(7)]
    carrier_id = 3
    if enemy_state == "carried":
        team[carrier_id] = replace(team[carrier_id], carried_entity_id=int(C.INTEL_PICKUP),
                                   position=(250.0, 250.0, 40.0))
    objectives = _ctf_objectives(
        own=_intel_state(own_state, own=True, holder=20),
        enemy=_intel_state(enemy_state, own=False, holder=carrier_id),
    )
    policy = _POLICIES[mode]
    for observer in team:
        others = [player for player in team if player is not observer]
        frame = _frame(mode, observer, *others, objectives=objectives)
        decision = policy.decide(frame, observer)
        assert decision is not None, (observer.player_id, own_state, enemy_state)
        assert decision.role


def test_last_wounded_arena_bot_keeps_hunting():
    wounded = replace(_player(1, BLUE, health=30), last_damage_at=NOW - 1.0)
    decision = _POLICIES["arena"].decide(_frame("arena", wounded, objectives=_anchors()), wounded)
    assert decision is not None
    assert decision.role == "arena_elimination_push"


def _mode_frames(mode, observer, *others):
    """Production-shaped frames: team anchors plus the mode's own objectives."""

    anchors = _anchors()
    yield _frame(mode, observer, *others, objectives=anchors)
    yield _frame(mode, observer, *others, objectives=anchors, phase="waiting")
    yield _frame(mode, observer, *others, objectives=anchors, phase="")
    extra = {
        "vip": (ObjectiveSnapshot("vip", BLUE, BLUE_HOME, carrier_id=observer.player_id),),
        "mh": (ObjectiveSnapshot("mh_hill", GREEN, (250.0, 250.0, 40.0), expires_in=3.0),),
        "dem": (ObjectiveSnapshot("dem_base", BLUE, BLUE_HOME),),
        "tc": (ObjectiveSnapshot("tc_territory", BLUE, BLUE_HOME),),
        "dia": (ObjectiveSnapshot("dia_dropoff", C.TEAM_NEUTRAL, (250.0, 250.0, 40.0), state=3),),
        "oc": (ObjectiveSnapshot("oc_target", GREEN, GREEN_HOME),),
    }.get(mode, ())
    if extra:
        for phase in ("active", "building", "airstrike", "countdown"):
            yield _frame(mode, observer, *others, objectives=(*anchors, *extra), phase=phase)


@pytest.mark.parametrize("mode", sorted(_MODE_STRATEGIES))
@pytest.mark.parametrize("team", [BLUE, GREEN])
def test_every_mode_has_an_order_for_a_bot_whatever_the_objectives_say(mode, team):
    observer = _player(4, team, (200.0, 200.0, 40.0))
    mate = _player(6, team, (210.0, 200.0, 40.0))
    for frame in _mode_frames(mode, observer, mate):
        # The policy itself, not only the worker's safety net behind it.
        decision = objective_decision_for(frame, observer)
        assert decision is not None and decision.role, (mode, frame.mode_phase)
    # Even with nothing published at all the worker's bot walks a beat.
    bare = ModePolicyMemory().decide(_frame(mode, observer), observer)
    assert bare is not None
    if not bare.role.endswith("_passive") and mode != "dia":
        assert bare.position != observer.position


# ------------------------------------------------------- CTF team split (F05)

def _blue_team(count=6, *, carrier=None):
    """Blue bots strung from home toward the enemy base, 60 blocks apart."""

    team = [_player(index * 2, BLUE, (60.5 + 60.0 * index, 256.5, 40.0))
            for index in range(count)]
    if carrier is not None:
        team[carrier] = replace(team[carrier], carried_entity_id=int(C.INTEL_PICKUP))
    return team


def _ctf_roles(team, objectives, *, mode="ctf", now=NOW, policy=None):
    policy = policy or _POLICIES[mode].__class__()
    roles = {}
    for observer in team:
        others = [player for player in team if player is not observer]
        decision = policy.decide(_frame(mode, observer, *others, objectives=objectives, now=now),
                                 observer)
        roles[observer.player_id] = decision
    return roles


def _count(roles, suffix):
    return sum(1 for decision in roles.values() if decision.role.endswith(suffix))


def test_ctf_team_keeps_a_home_guard_and_escorts_while_a_teammate_carries():
    team = _blue_team(carrier=4)   # the carrier is 240 blocks out
    carried = (team[4].player_id, 2, team[4].position)
    roles = _ctf_roles(team, _ctf_objectives(enemy=carried))
    assert roles[team[4].player_id].role == "ctf_capture"
    assert roles[team[0].player_id].role == "ctf_defend"    # the bot at home stays
    assert _count(roles, "ctf_defend") == 1
    assert _count(roles, "ctf_escort") == 2
    assert {team[3].player_id, team[5].player_id} == {
        player_id for player_id, decision in roles.items() if decision.role == "ctf_escort"}
    assert _count(roles, "ctf_escort_cover") == 2
    # The escort answers anyone who can hit the carrier, not only point blank.
    assert roles[team[3].player_id].engagement_radius >= 50.0


def test_ctf_cover_goes_ahead_of_the_carrier_from_home_and_behind_it_from_the_raid():
    team = _blue_team(8, carrier=4)
    carried = (team[4].player_id, 2, team[4].position)
    roles = _ctf_roles(team, _ctf_objectives(enemy=carried))
    carrier_x = team[4].position[0]
    front = roles[team[2].player_id]     # between the carrier and home
    rear = roles[team[7].player_id]      # deeper in enemy ground than the carrier
    assert front.role == rear.role == "ctf_escort_cover"
    assert front.position[0] < carrier_x < rear.position[0]


def test_ctf_team_splits_between_the_hunt_and_the_raid_when_its_intel_is_taken():
    team = _blue_team()
    policy = _POLICIES["ctf"].__class__()
    _ctf_roles(team, _ctf_objectives(), policy=policy)                 # intel seen at home
    stolen = (21, 2, (70.0, 256.5, 40.0))
    roles = _ctf_roles(team, _ctf_objectives(own=stolen), policy=policy, now=NOW + 1.0)
    assert _count(roles, "ctf_intercept_carrier") == 3
    assert _count(roles, "ctf_defend") == 0      # nothing left at home to guard
    raiders = [decision for decision in roles.values()
               if decision.role in ("ctf_attack_intel", "ctf_rally")]
    assert len(raiders) == 3
    # The three nearest the theft hunt; the far end of the team keeps raiding.
    assert roles[team[0].player_id].role == "ctf_intercept_carrier"
    assert roles[team[5].player_id].role == "ctf_attack_intel"


def test_ctf_raider_at_the_enemy_intel_is_not_called_back_to_hunt():
    # Two bots, both nearer the thief's road than anyone else: the one about
    # to take the enemy intel finishes the raid, the other hunts.
    raider = _player(0, BLUE, (GREEN_HOME[0] - 30.0, 256.5, 40.0))
    other = _player(2, BLUE, (GREEN_HOME[0] - 120.0, 256.5, 40.0))
    policy = _POLICIES["ctf"].__class__()
    _ctf_roles([raider, other], _ctf_objectives(), policy=policy)
    stolen = _ctf_objectives(own=(21, 2, (GREEN_HOME[0] - 40.0, 256.5, 40.0)))
    roles = _ctf_roles([raider, other], stolen, policy=policy, now=NOW + 40.0)
    assert roles[raider.player_id].role == "ctf_attack_intel"
    assert roles[other.player_id].role == "ctf_intercept_carrier"


def test_ctf_escort_and_hunt_share_the_team_when_both_intels_are_carried():
    team = _blue_team(carrier=2)
    policy = _POLICIES["ctf"].__class__()
    _ctf_roles(_blue_team(), _ctf_objectives(), policy=policy)
    both = _ctf_objectives(own=(21, 2, (300.0, 256.5, 40.0)),
                           enemy=(team[2].player_id, 2, team[2].position))
    roles = _ctf_roles(team, both, policy=policy, now=NOW + 1.0)
    assert _count(roles, "ctf_capture") == 1
    assert _count(roles, "ctf_escort") == 2
    assert _count(roles, "ctf_intercept_carrier") == 3
    assert _count(roles, "ctf_defend") == 0


@pytest.mark.parametrize("size,guards", [(1, 0), (2, 0), (3, 1), (6, 1), (7, 2), (10, 2)])
def test_ctf_home_guard_grows_with_the_team(size, guards):
    roles = _ctf_roles(_blue_team(size), _ctf_objectives())
    assert _count(roles, "ctf_defend") == guards
    assert all(decision is not None for decision in roles.values())


@pytest.mark.parametrize("own", [(-1, 1, (200.0, 256.5, 40.0)), (21, 2, (200.0, 256.5, 40.0))])
def test_ctf_nobody_guards_a_base_whose_intel_is_gone(own):
    roles = _ctf_roles(_blue_team(), _ctf_objectives(own=own))
    assert _count(roles, "ctf_defend") == 0
    if own[0] < 0:
        assert _count(roles, "ctf_recover_intel") == 2


def test_ctf_hunters_only_learn_the_thief_position_once_the_marker_shows():
    # The minimap marks a carrier after INTEL_MINIMAP_EXPOSURE_TIME (30 s).
    # Until then a robbed team knows where the intel was taken and where it
    # must go, not where the thief stands.
    observer = _player(0, BLUE, (200.0, 256.5, 40.0))
    policy = _POLICIES["ctf"].__class__()
    policy.decide(_frame("ctf", observer, objectives=_ctf_objectives()), observer)
    thief_at = (140.0, 330.0, 40.0)   # well off the line between the bases
    stolen = _ctf_objectives(own=(21, 2, thief_at))
    early = policy.decide(_frame("ctf", observer, objectives=stolen, now=NOW + 1.0), observer)
    later = policy.decide(_frame("ctf", observer, objectives=stolen, now=NOW + 11.0), observer)
    assert early.role == later.role == "ctf_intercept_carrier"
    for decision in (early, later):
        assert decision.position != thief_at
        assert abs(decision.position[1] - BLUE_HOME[1]) < 1.0   # on the thief's road home
    assert BLUE_HOME[0] <= early.position[0] < later.position[0] < GREEN_HOME[0]
    marked = policy.decide(_frame("ctf", observer, objectives=stolen, now=NOW + 32.0), observer)
    assert marked.position == thief_at


def test_classic_team_never_hunts_its_hidden_thief():
    # No minimap, no marker: the raid on the enemy base (where the thief has
    # to score) goes on, and nobody is steered by the thief's position.
    team = _blue_team()
    policy = _POLICIES["cctf"].__class__()
    _ctf_roles(team, _ctf_objectives(), mode="cctf", policy=policy)
    thief_at = (140.0, 330.0, 40.0)
    stolen = _ctf_objectives(own=(21, 2, thief_at))
    for seconds in (1.0, 20.0, 45.0, 240.0):
        roles = _ctf_roles(team, stolen, mode="cctf", policy=policy, now=NOW + seconds)
        assert _count(roles, "ctf_intercept_carrier") == 0
        for decision in roles.values():
            assert decision.role in ("classic_ctf_attack_intel", "classic_ctf_rally")
            assert math.dist(decision.position, thief_at) > 50.0


def test_ctf_hunt_of_a_thief_first_seen_carrying_waits_at_the_base_it_must_reach():
    # The policy met this intel already in a thief's hands (a worker restart):
    # it has no theft spot to reckon from until the marker shows.
    observer = _player(0, BLUE, (200.0, 256.5, 40.0))
    policy = _POLICIES["ctf"].__class__()
    thief_at = (140.0, 330.0, 40.0)
    stolen = _ctf_objectives(own=(21, 2, thief_at))
    early = policy.decide(_frame("ctf", observer, objectives=stolen), observer)
    assert early.role == "ctf_intercept_carrier"
    assert early.position == GREEN_HOME
    marked = policy.decide(_frame("ctf", observer, objectives=stolen, now=NOW + 31.0), observer)
    assert marked.position == thief_at


def _raid_frame(observer, *others, now, hit_at=0.0):
    observer = replace(observer, last_damage_at=hit_at)
    return _frame("ctf", observer, *others, objectives=_ctf_objectives(), now=now), observer


# A moment just after Blue's wave left: the next one is 14 s away.
_AFTER_WAVE = NOW - (NOW + 7.0 * BLUE) % 20.0 + 6.0


def test_ctf_raiders_stack_up_outside_the_enemy_base_and_go_in_together():
    policy = _POLICIES["ctf"].__class__()
    first = _player(0, BLUE, (GREEN_HOME[0] - 70.0, 256.5, 40.0))
    coming = _player(2, BLUE, (GREEN_HOME[0] - 150.0, 256.5, 40.0))
    frame, observer = _raid_frame(first, coming, now=_AFTER_WAVE)
    waiting = policy.decide(frame, observer)
    assert waiting.role == "ctf_rally" and waiting.position == first.position
    # Three in the band go at once.
    second = replace(coming, position=(GREEN_HOME[0] - 75.0, 250.0, 40.0))
    third = _player(4, BLUE, (GREEN_HOME[0] - 80.0, 262.0, 40.0))
    frame, observer = _raid_frame(first, second, third, now=_AFTER_WAVE)
    assert policy.decide(frame, observer).role == "ctf_attack_intel"
    # Anyone follows a push that is already inside.
    inside = replace(coming, position=(GREEN_HOME[0] - 30.0, 256.5, 40.0))
    frame, observer = _raid_frame(first, inside, now=_AFTER_WAVE)
    assert policy.decide(frame, observer).role == "ctf_attack_intel"
    # Nobody waits under fire.
    frame, observer = _raid_frame(first, coming, now=_AFTER_WAVE, hit_at=_AFTER_WAVE - 1.0)
    assert policy.decide(frame, observer).role == "ctf_attack_intel"


def test_ctf_wave_wait_is_bounded_by_the_shared_clock():
    policy = _POLICIES["ctf"].__class__()
    first = _player(0, BLUE, (GREEN_HOME[0] - 70.0, 256.5, 40.0))
    roles = []
    for step in range(40):       # 20 s at 2 Hz
        frame, observer = _raid_frame(first, now=_AFTER_WAVE + step * 0.5)
        roles.append(policy.decide(frame, observer).role)
    assert roles[0] == "ctf_rally"
    waits = "".join("w" if role == "ctf_rally" else " " for role in roles).split()
    assert max(len(run) for run in waits) * 0.5 <= 15.0
    assert "ctf_attack_intel" in roles


def test_ctf_dropped_enemy_intel_is_fetched_without_waiting_for_a_wave():
    policy = _POLICIES["ctf"].__class__()
    observer = _player(0, BLUE, (GREEN_HOME[0] - 70.0, 256.5, 40.0))
    dropped = _ctf_objectives(enemy=(-1, 1, (GREEN_HOME[0] - 5.0, 256.5, 40.0)))
    decision = policy.decide(
        _frame("ctf", observer, objectives=dropped, now=_AFTER_WAVE), observer)
    assert decision.role == "ctf_attack_intel"


def test_ctf_raider_does_not_stutter_between_waiting_and_going():
    # The conditions of a wait flicker as teammates cross the band edges; a
    # third of all waits used to last half a second.
    memory = ModePolicyMemory()
    first = _player(0, BLUE, (GREEN_HOME[0] - 70.0, 256.5, 40.0))
    coming = _player(2, BLUE, (GREEN_HOME[0] - 150.0, 256.5, 40.0))
    inside = replace(coming, position=(GREEN_HOME[0] - 59.0, 256.5, 40.0))

    def role(at, *others, hit_at=0.0):
        frame, observer = _raid_frame(first, *others, now=at, hit_at=hit_at)
        return memory.decide(frame, observer).role

    assert role(_AFTER_WAVE, coming) == "ctf_rally"
    # A teammate brushing the inner edge half a second later does not end it...
    assert role(_AFTER_WAVE + 0.5, inside) == "ctf_rally"
    # ...but the wait ends once it has lasted, and at once under fire.
    assert role(_AFTER_WAVE + 2.0, inside) == "ctf_attack_intel"
    # Having set off, the raider does not stop again a moment later.
    assert role(_AFTER_WAVE + 2.5, coming) == "ctf_attack_intel"
    assert role(_AFTER_WAVE + 6.0, coming) == "ctf_rally"
    assert role(_AFTER_WAVE + 6.5, inside, hit_at=_AFTER_WAVE + 6.2) == "ctf_attack_intel"
