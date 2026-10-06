"""Mode objective roles: every live bot has an order and teams split the work."""

from __future__ import annotations

from dataclasses import replace
import itertools

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
