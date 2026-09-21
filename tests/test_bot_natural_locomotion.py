"""Movement continuity and objective ownership at real worker boundaries."""

from dataclasses import replace
import pytest

from server.bot_ai.locomotion import CombatFootwork
from server.bot_ai.messages import BotActionKind, MovementAffordance, MovementIntent, ObjectiveSnapshot
from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
from server.bot_ai.simple_navigation import RouteStep
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState
from tests.test_simple_bot_tactics import _TacticalWorld, _frame, _player, _profile


def test_stride_does_not_rotate_with_every_small_target_motion():
    footwork = CombatFootwork()
    actor = _player(1, 2, (10., 10., 20.), is_bot=True)
    enemy = _player(2, 3, (35., 10., 20.))
    first = footwork.direction(actor, enemy, _profile(), 100.)
    for index in range(1, 5):
        moved_enemy = replace(enemy, position=(35., 10. + index * .3, 20.))
        assert footwork.direction(actor, moved_enemy, _profile(), 100. + index * .1) == first


def test_actual_stride_distance_triggers_a_bounded_firing_pause():
    footwork = CombatFootwork()
    actor = _player(1, 2, (10., 10., 20.), is_bot=True)
    enemy = _player(2, 3, (35., 10., 20.))
    heading = footwork.direction(actor, enemy, _profile(), 100.)
    moved = replace(actor, position=tuple(actor.position[i] + heading[i] * 6 for i in range(3)))
    assert footwork.direction(moved, enemy, _profile(), 100.3) == (0., 0., 0.)
    resume = footwork.until
    assert 100.3 < resume < 101.5
    assert footwork.direction(moved, enemy, _profile(), resume + .01) != (0., 0., 0.)


def test_range_boundaries_do_not_alternate_approach_and_retreat():
    footwork = CombatFootwork()
    assert footwork.spacing(41., 10., 40.) == "approach"
    for distance in (39.8, 40.2, 39.9, 38.):
        assert footwork.spacing(distance, 10., 40.) == "approach"
    assert footwork.spacing(34., 10., 40.) == "hold"
    assert footwork.spacing(6., 10., 40.) == "retreat"
    for distance in (6.6, 6.4, 8.):
        assert footwork.spacing(distance, 10., 40.) == "retreat"
    assert footwork.spacing(10., 10., 40.) == "hold"


def test_received_pressure_repositions_without_restarting_every_frame():
    footwork = CombatFootwork()
    actor = _player(1, 2, (10., 10., 20.), is_bot=True)
    enemy = _player(2, 3, (35., 10., 20.))
    footwork.direction(actor, enemy, _profile(), 100.)
    hit = replace(actor, health=40, last_damage_at=100.7)
    evasive = footwork.direction(hit, enemy, _profile(), 100.8)
    assert evasive[0] < -.3
    assert footwork.direction(replace(hit, last_damage_at=100.85), enemy,
                              _profile(), 100.9) == evasive


def test_identity_variation_is_repeatable_and_roster_is_not_synchronized():
    schedules = []
    for identifier in range(8):
        actor = _player(identifier, 2, (10., 10., 20.), is_bot=True)
        enemy = _player(99, 3, (35., 10., 20.))
        first, second = CombatFootwork(), CombatFootwork()
        assert first.direction(actor, enemy, _profile(), 100.) == second.direction(
            actor, enemy, _profile(), 100.)
        schedules.append((round(first.until, 3), round(first.stride_length, 3)))
    assert len(set(schedules)) == 8


@pytest.mark.parametrize("role,posture", [
    ("vip_guard_formation", ModeBotPosture.ESCORT),
    ("ctf_capture", ModeBotPosture.EVASIVE),
    ("multihill_defend", ModeBotPosture.DEFEND),
    ("vip_flank_attack", ModeBotPosture.ASSAULT),
    ("diamond_collect", ModeBotPosture.BALANCED),
])
def test_incidental_combat_retains_the_existing_objective_route(monkeypatch, role, posture):
    actor = _player(1, 2, (10., 10., 20.), is_bot=True)
    enemy = _player(2, 3, (30., 10., 20.))
    frame = _frame(actor, enemy)
    decision = ModeBotDecision((10., 60., 20.), role, posture=posture,
                               objective_priority=.94)
    brain = SimpleBotBrain(_TacticalWorld())
    state = _BotState(1, 1, 0)
    goal = brain._goal_from_mode_decision(decision)
    brain._set_goal(state, goal, actor.position, 98.)
    state.route = (RouteStep((10., 15., 20.), MovementAffordance.WALK),)
    saved_route = state.route
    calls = []

    def navigate(_frame, _actor, current, requested, _now):
        calls.append(requested)
        assert current.goal == goal
        assert current.route is saved_route
        assert current.goal_progress_at == 98.
        return brain._intent(frame, movement=MovementIntent(direction=(0., 1., 0.)),
                             look=None, tool_id=actor.tool, debug_goal=requested.position,
                             debug_role=requested.role)

    monkeypatch.setattr(brain, "_navigation_intent", navigate)
    intent = brain._combat_intent(frame, actor, enemy, state, _profile(), 100., decision)
    assert len(calls) == 1
    assert intent.movement.direction == (0., 1., 0.)
    assert intent.look.target_player_id == enemy.player_id
    assert intent.action.kind is BotActionKind.FIRE
    assert intent.debug_goal == decision.position


def test_deliberate_firing_pause_does_not_trigger_blocked_recovery():
    state = _BotState(1, 1, 0, combat_wants_movement=False)
    for now in (100., 100.2, 101., 102.):
        SimpleBotBrain._update_combat_progress(state, (10., 10., 20.), now)
        assert state.combat_stall_stage == 0
    state.combat_wants_movement = True
    for now in (102.2, 102.4, 102.6, 102.8, 103.1):
        SimpleBotBrain._update_combat_progress(state, (10., 10., 20.), now)
    assert state.combat_stall_stage == 1


def test_visible_vip_preempts_a_decoy_but_a_marker_does_not_bypass_los():
    actor = _player(1, 2, (10., 10., 20.), is_bot=True)
    decoy = _player(2, 3, (25., 10., 20.))
    vip = _player(3, 3, (40., 10., 20.))
    frame = replace(_frame(actor, decoy, vip), mode_id="vip",
                    objectives=(ObjectiveSnapshot("vip", 3, vip.position, carrier_id=3),))
    state = _BotState(1, 1, 0, contact_id=2, contact_generation=1)
    brain = SimpleBotBrain(_TacticalWorld())
    assert brain._visible_target(frame, actor, state).player_id == 3
    brain.world.visible = False
    assert brain._visible_target(frame, actor, state) is None


def test_arrived_guard_watches_approach_at_eye_height_without_wandering():
    actor = _player(1, 2, (10., 10., 20.), is_bot=True)
    brain = SimpleBotBrain(_TacticalWorld())
    decision = ModeBotDecision(actor.position, "vip_guard_formation",
                               watch_position=(80., 10., 90.))
    intent = brain._navigation_intent(_frame(actor), actor, _BotState(1, 1, 0),
                                      brain._goal_from_mode_decision(decision), 100.)
    assert intent.movement.direction == (0., 0., 0.)
    assert intent.look.target[2] == actor.eye[2]
    assert intent.look.target[0] > actor.eye[0]


def test_new_close_threat_preempts_a_tracked_visible_vip():
    actor = _player(1, 2, (10., 10., 20.), is_bot=True)
    close = _player(2, 3, (12., 10., 20.))
    vip = _player(3, 3, (40., 10., 20.))
    frame = replace(_frame(actor, close, vip), mode_id="vip",
                    objectives=(ObjectiveSnapshot("vip", 3, vip.position, carrier_id=3),))
    state = _BotState(1, 1, 0, contact_id=3, contact_generation=1)
    assert SimpleBotBrain(_TacticalWorld())._visible_target(frame, actor, state) == close


@pytest.mark.parametrize("already_reloading", [False, True])
def test_reload_repositions_away_instead_of_charging_at_the_enemy(already_reloading):
    actor = _player(1, 2, (10., 10., 20.), is_bot=True)
    enemy = _player(2, 3, (35., 10., 20.))
    state = _BotState(1, 1, 0)
    # A reload starting during a committed offensive stride must interrupt it.
    state.footwork.direction(actor, enemy, _profile(), 99.9)
    actor = replace(actor, ammo_clip=0, reloading=already_reloading)
    intent = SimpleBotBrain(_TacticalWorld())._combat_intent(
        _frame(actor, enemy), actor, enemy, state, _profile(), 100.)
    assert intent.movement.direction[0] < -.3
    assert not intent.movement.sprint
    assert intent.action.kind is (BotActionKind.NONE if already_reloading else BotActionKind.RELOAD)
