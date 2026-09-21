"""Internal action transactions cannot repeat commits or cross a respawn."""

from types import SimpleNamespace
from dataclasses import replace
import random

from server.bot_ai.director import BotDirector, _AimMotor, _RuntimeBot, _player_life_id
from server.bot_ai.messages import BotAction, BotActionKind, BotIntent, MovementIntent
from tests.test_simple_bot_tactics import _profile


def runtime():
    return SimpleNamespace(player=SimpleNamespace(deaths=0), committed_requests={},
        committed_request_life=-1, committed_request_high_water=0)


def test_task_retries_and_evicted_old_requests_cannot_commit_twice():
    director = object.__new__(BotDirector)
    calls = []
    director.gateway = SimpleNamespace(execute=lambda player, action: calls.append(action) or True)
    state = runtime()
    first = BotAction(BotActionKind.BUILD, request_id=1)
    assert director._execute_task_action(state, first)
    assert director._execute_task_action(state, first)
    assert len(calls) == 1
    for request in range(2, 100):
        assert director._execute_task_action(state, BotAction(BotActionKind.BUILD, request_id=request))
    assert len(state.committed_requests) == 64
    assert not director._execute_task_action(state, first)
    assert len(calls) == 99


def test_rejected_transaction_stays_rejected_even_after_resources_change():
    director = object.__new__(BotDirector)
    calls = []
    director.gateway = SimpleNamespace(execute=lambda player, action: calls.append(action) and False)
    state = runtime()
    action = BotAction(BotActionKind.DEPLOY, request_id=17)
    assert not director._execute_task_action(state, action)
    director.gateway.execute = lambda player, action: calls.append(action) or True
    assert not director._execute_task_action(state, action)
    assert len(calls) == 1
    state.player.deaths += 1
    assert director._execute_task_action(state, action)
    assert len(calls) == 2


def test_combat_feedback_cannot_overwrite_pending_task_acknowledgement():
    state = runtime()
    state.last_action_frame = 25
    action = BotAction(BotActionKind.PLACE_PREFAB, request_id=20)
    BotDirector._record_action_result(state, action, True, 100)
    BotDirector._record_action_result(state, BotAction(BotActionKind.FIRE), False, 100.1)
    assert state.feedback_request_id == 20
    assert state.feedback_task_accepted and state.feedback_task_at == 100
    assert state.feedback_action_kind == "fire" and not state.feedback_action_accepted


def test_non_death_respawn_has_a_distinct_action_life():
    player = SimpleNamespace(deaths=2, replication_generation=4)
    previous = _player_life_id(player)
    player.replication_generation += 1
    assert _player_life_id(player) != previous and player.deaths == 2


def test_director_rejects_delayed_previous_spawn_intent_but_accepts_current_life():
    director = object.__new__(BotDirector)
    actor = SimpleNamespace(id=1, replication_generation=2, deaths=0)
    state = _RuntimeBot(actor, 1, _profile(), _AimMotor(0.0), random.Random(1))
    director._runtime = {1: state}
    director._map_epoch, director._mode_epoch, director._topology_version = 1, 1, 0
    delayed = BotIntent(1, 1, 10, 1, 1, 0, 100, 101, MovementIntent(), life_id=1)
    director.supervisor = SimpleNamespace(drain_intents=lambda **_: [delayed])
    director._drain_intents(100.2)
    assert state.intent is None
    director.supervisor.drain_intents = lambda **_: [replace(delayed, frame_id=11, life_id=2)]
    director._drain_intents(100.3)
    assert state.intent.life_id == 2


def test_pending_aim_action_cannot_survive_non_death_respawn():
    director = object.__new__(BotDirector)
    actor = SimpleNamespace(id=1, replication_generation=2, deaths=0, alive=True, spawned=True)
    state = _RuntimeBot(actor, 1, _profile(), _AimMotor(0.0), random.Random(1))
    state.pending_action = BotAction(BotActionKind.FIRE)
    state.pending_action_life_id, state.pending_action_deadline = 1, 101
    director._try_pending_action(state, 100.2)
    assert state.pending_action is None
