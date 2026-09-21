"""An authoritative objective pickup retires weapon work before worker refresh."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import shared.constants as C

from server.bot_ai.messages import BotAction, BotActionKind, BotIntent, LookIntent, MovementAffordance, MovementIntent
from tests.test_bot_architecture import _facing_fixture


def test_snapshot_weapon_permission_matches_live_burden_and_mode_rule():
    server, director, bot, _ = _facing_fixture()
    for burdensome, mode, expected in (
        (False, None, True),
        (True, None, False),
        (True, SimpleNamespace(shoot_with_intel=False), False),
        (True, SimpleNamespace(shoot_with_intel=True), True),
    ):
        bot.pickup_burdensome = burdensome
        server.mode = mode
        assert director._snapshot_player(bot).can_shoot is expected


@pytest.mark.parametrize("kind", (BotActionKind.FIRE, BotActionKind.MELEE))
@pytest.mark.parametrize("expired", (False, True))
def test_pickup_cancels_pending_and_queued_weapon_work_before_aim(kind, expired, monkeypatch):
    server, director, bot, runtime = _facing_fixture()
    now = 100.
    tool = int(C.RIFLE_TOOL if kind is BotActionKind.FIRE else C.SUPERSPADE_TOOL)
    action = BotAction(kind, tool, position=(bot.eye_x, bot.eye_y + 1., bot.eye_z + 6.), burst=3)
    intent = BotIntent(bot.id, runtime.generation, 1, 0, 0, 0, now - .1,
                       now - .01 if expired else now + 1.,
                       MovementIntent(affordance=MovementAffordance.BREACH),
                       look=LookIntent(action.position, visible=True), tool_id=tool,
                       action=action, secondary_fire=True, zoom=True)
    runtime.intent = intent
    director._latch_action(runtime, intent)
    runtime.burst_remaining = 2
    runtime.action_primary_until_loop = 500
    runtime.lock_player_id = 4
    director._set_action_state(runtime, primary=True, secondary=True, zoom=True, hover=False)
    director._pending_gateway_actions[bot.id] = (runtime.generation, action, now)
    executed = []
    monkeypatch.setattr(director.gateway, "execute", lambda *_args: executed.append(True))
    bot.pickup_burdensome = True
    server.mode = SimpleNamespace(shoot_with_intel=False)

    # A pickup can occur between staggered motor ticks. Physics observation
    # retires the old shot and its replicated primary bit immediately.
    director.observe_player_physics(bot, now)
    assert runtime.pending_action is None
    assert not director._pending_gateway_actions
    assert runtime.burst_remaining == 0
    assert runtime.action_primary_until_loop == -1
    assert not bot.input.primary_fire
    assert director.drain_actions() == 0
    director._apply_motor(runtime, now, .1)
    assert runtime.motor.pitch == pytest.approx(0.)
    assert runtime.motor.yaw == pytest.approx(0.)
    assert runtime.pending_action is None
    assert not executed


def test_breach_cooldown_look_is_removed_but_legal_build_intent_is_preserved(monkeypatch):
    server, director, bot, runtime = _facing_fixture()
    now = 100.
    bot.pickup_burdensome = True
    server.mode = SimpleNamespace(shoot_with_intel=False)
    runtime.intent = BotIntent(bot.id, runtime.generation, 1, 0, 0, 0, now, now + 1.,
                               MovementIntent(affordance=MovementAffordance.BREACH),
                               look=LookIntent((bot.eye_x, bot.eye_y + 1., bot.eye_z + 5.)))
    director._apply_motor(runtime, now, .1)
    assert runtime.intent.look is None
    assert runtime.motor.pitch == pytest.approx(0.)
    # A tool switch does not make a stale firing pulse become a build pulse.
    bot.set_tool(int(C.BLOCK_TOOL))
    runtime.intent = replace(runtime.intent, frame_id=2,
                             action=BotAction(BotActionKind.FIRE, int(C.RIFLE_TOOL)))
    runtime.action_primary_until_loop = 500
    director._set_action_state(runtime, primary=True, hover=False)
    director.observe_player_physics(bot, now + .05)
    assert runtime.action_primary_until_loop == -1
    assert not bot.input.primary_fire
    build = BotAction(BotActionKind.BUILD, int(C.BLOCK_TOOL), position=(10., 10., 60.))
    runtime.intent = replace(runtime.intent, frame_id=3, action=build,
                             movement=MovementIntent(affordance=MovementAffordance.BUILD_STEP))
    executed = []
    monkeypatch.setattr(director.gateway, "execute", lambda _player, action: executed.append(action) or True)
    director._apply_motor(runtime, now + .1, .1)
    assert executed == [build]


@pytest.mark.parametrize("allowed", (False, True))
def test_commit_boundary_rechecks_pickup_without_waiting_for_motor(allowed, monkeypatch):
    server, director, bot, runtime = _facing_fixture()
    action = BotAction(BotActionKind.FIRE, int(C.RIFLE_TOOL))
    runtime.pending_action = action
    runtime.pending_action_deadline = 101.
    bot.pickup_burdensome = True
    server.mode = SimpleNamespace(shoot_with_intel=allowed)
    executed = []
    monkeypatch.setattr(director.gateway, "execute", lambda _player, value: executed.append(value) or True)
    director._commit_pending_action(runtime, action, 100.)
    assert executed == ([action] if allowed else [])
    if allowed:
        assert runtime.action_primary_kind is BotActionKind.FIRE
        assert runtime.pending_action is None and runtime.intent is None
        bot.set_tool(int(C.BLOCK_TOOL))
        server.mode.shoot_with_intel = False
        director.observe_player_physics(bot, 100.01)
        assert runtime.action_primary_until_loop == -1
        assert runtime.action_primary_kind is BotActionKind.NONE
        assert not bot.input.primary_fire


@pytest.mark.parametrize("kind", (BotActionKind.ORIENTED, BotActionKind.DEPLOY))
def test_non_shot_action_is_not_cancelled_by_carrier_weapon_fence(kind):
    server, director, bot, runtime = _facing_fixture()
    action = BotAction(kind, int(C.GRENADE_TOOL))
    bot.set_tool(int(C.GRENADE_TOOL))
    runtime.pending_action = action
    runtime.action_primary_kind = kind
    runtime.action_primary_until_loop = 500
    director._pending_gateway_actions[bot.id] = (runtime.generation, action, 100.)
    bot.pickup_burdensome = True
    server.mode = SimpleNamespace(shoot_with_intel=False)
    director._enforce_weapon_capability(runtime)
    assert runtime.pending_action is action
    assert director._pending_gateway_actions[bot.id][1] is action
    assert runtime.action_primary_kind is kind
    assert runtime.action_primary_until_loop == 500
