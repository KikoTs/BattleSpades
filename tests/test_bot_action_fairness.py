"""A full roster keeps its action turns across ordinary worker refreshes."""

import asyncio
from collections import Counter
from types import SimpleNamespace

import shared.constants as C
from server.bot_ai.director import BotDirector
from server.bot_ai.messages import BotAction, BotActionKind
from server.config import ServerConfig
from server.main import BattleSpadesServer


def test_refreshing_validated_shots_cannot_starve_a_full_roster(monkeypatch):
    async def scenario():
        config = ServerConfig()
        config.max_players = 32
        config.bots.max_bots = 24
        server = BattleSpadesServer(config)
        server.world_manager.generate_flat_map()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        for _ in range(24):
            assert await director.add_bot(team=2) is not None
        director._started = True
        shots = Counter()

        def commit(runtime, action, now):
            shots[runtime.player.id] += 1
            director._clear_pending_action(runtime)

        monkeypatch.setattr(director, "_commit_pending_action", commit)
        for tick in range(120):
            now = 100. + tick / 60.
            for runtime in director._runtime.values():
                if tick % 8 == 0:
                    # Each worker decision owns a fresh command object.
                    runtime.pending_action = BotAction(BotActionKind.FIRE, int(C.SMG_TOOL))
                if runtime.player.id % 2 == tick % 2 and runtime.pending_action is not None:
                    # This boundary is reached only after current aim,
                    # visibility and fire-lane validation in the motor.
                    director._execute_pending(runtime, runtime.pending_action, now)
            director.drain_actions(limit=1)
        assert set(shots) == {bot.id for bot in director.bots}
        assert min(shots.values()) >= 2
        assert sum(shots.values()) <= 120  # The fixed-step budget is unchanged.
        director._started = False

    asyncio.run(scenario())


def test_a_new_command_must_be_validated_before_using_its_held_queue_position(monkeypatch):
    from tests.test_bot_architecture import _facing_fixture

    _, director, _, runtime = _facing_fixture()
    director._started = True
    old = BotAction(BotActionKind.FIRE, int(C.RIFLE_TOOL))
    replacement = BotAction(BotActionKind.MELEE, int(C.SPADE_TOOL))
    executed = []
    monkeypatch.setattr(director, "_commit_pending_action",
                        lambda rt, action, now: executed.append(action))
    runtime.pending_action = old
    director._execute_pending(runtime, old, 100.)
    runtime.pending_action = replacement
    assert director.drain_actions() == 0
    assert not executed
    director._execute_pending(runtime, replacement, 100.1)
    assert director.drain_actions() == 1
    assert executed == [replacement]
    director._started = False
