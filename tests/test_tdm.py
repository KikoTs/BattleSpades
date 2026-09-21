"""TDM scoring + the kill/death event-dispatch pipeline.

Exercises the path that was dead before: Player.die() queues on_player_kill /
on_player_death, the loop drains them into the mode, TDM scores and ends the
match. Uses a light server stub (the real BattleSpadesServer needs ENet); the
Player/Team/world objects are real.
"""
import asyncio
from collections import deque
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.game_constants import KILL_HEADSHOT, KILL_TEAM_CHANGE, TEAM1, TEAM2  # noqa: E402
from server.player import Player  # noqa: E402
from server.team import Team  # noqa: E402
from server.world_manager import WorldManager  # noqa: E402
from modes.tdm import TDMMode  # noqa: E402


class _Conn:
    def __init__(self, server):
        self.server = server
        self.player = None

    def send(self, data, reliable=True, prefix=0x30):
        pass


class _TDMServer:
    """Minimal stand-in for BattleSpadesServer with the mode-event plumbing."""

    def __init__(self):
        self.config = ServerConfig()
        self.config.default_mode = "tdm"
        self.config.log_suppress_packets = set()
        self.tick_rate = self.config.tick_rate
        self.players = {}
        self.connections = {}
        self.teams = {
            TEAM1: Team(TEAM1, "Blue", (0, 0, 255)),
            TEAM2: Team(TEAM2, "Green", (0, 255, 0)),
        }
        self.world_manager = WorldManager(self.config)
        self.world_manager.generate_flat_map()
        self._mode_events = []
        self.broadcast_packets = []
        self.state_pushes = 0
        self.mode = None

    def queue_mode_event(self, name, *args):
        self._mode_events.append((name, args))

    def broadcast(self, data, exclude=None):
        self.broadcast_packets.append(data)

    def broadcast_state_data(self):
        self.state_pushes += 1

    def broadcast_set_score(self, team):
        # TDM pushes scores via the lightweight SetScore packet now; count it
        # the same way the old StateData push was counted.
        self.state_pushes += 1
        self.last_score = (team.id, team.score)

    async def drain(self):
        """Mirror _game_loop's post-on_tick drain."""
        events = self._mode_events
        self._mode_events = []
        for name, args in events:
            handler = getattr(self.mode, name, None)
            if handler is not None:
                await handler(*args)


def _make_player(server, pid, name, team):
    conn = _Conn(server)
    p = Player(pid, name, team, C.RIFLE_TOOL, conn)
    conn.player = p
    p.spawn(100.5 + pid, 100.5, 60.0)
    server.players[pid] = p
    server.connections[pid] = conn
    server.teams[team].add_player(p)
    return p


def _new_match():
    server = _TDMServer()
    mode = TDMMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    return server, mode


def test_cross_team_kill_scores_one_point():
    server, mode = _new_match()
    killer = _make_player(server, 0, "Killer", TEAM1)
    victim = _make_player(server, 1, "Victim", TEAM2)

    victim.damage(999, source=killer, kill_type=0)
    asyncio.run(server.drain())

    assert server.teams[TEAM1].score == mode.kill_points
    assert server.teams[TEAM2].score == 0
    # A cross-team kill broadcasts two SetScore(85) packets: the killer's
    # personal score (type=PLAYER) and the team's bar (type=TEAM).
    setscores = [d for d in server.broadcast_packets if d and d[0] == 85]
    assert len(setscores) == 2
    assert killer.kills == 1
    assert killer.score == 100


@pytest.mark.parametrize("event_budget,backlog", [(512, 0), (1, 0), (512, 512)])
def test_accepted_final_tick_kill_is_scored_before_time_limit_winner(event_budget, backlog):
    from server.simulation_runtime import SimulationRuntime

    server, mode = _new_match()
    killer = _make_player(server, 0, "Killer", TEAM1)
    victim = _make_player(server, 1, "Victim", TEAM2)
    mode.time_limit = 1
    mode.start_time = 0.0
    mode._timeout_music_played = True
    server.loop_count = 1
    server._mode_events = deque()
    server.config.mode_event_drain_budget = event_budget
    server._mode_events.extend(("on_test_backlog", ()) for _ in range(backlog))
    plugin_calls = []

    async def plugin_event(name, *_args):
        plugin_calls.append(name)

    async def no_delayed_transition(_winner):
        pass

    server.plugin_manager = SimpleNamespace(call_event=plugin_event)
    mode._run_end_sequence = no_delayed_transition
    # The kill is accepted before the runtime evaluates this frame's clock.
    # Calling on_tick first used to end a 0-0 draw and then discard both
    # scoring events, leaving killer.kills=1 with score/team score still zero.
    victim.damage(999, source=killer)
    runtime = SimulationRuntime(server)

    async def drain_before_expiry():
        while server._mode_events:
            before = len(server._mode_events)
            await runtime._tick_mode()
            assert before - len(server._mode_events) <= event_budget
            if server._mode_events:
                assert not mode.ended
            server.loop_count += 1

    asyncio.run(drain_before_expiry())

    assert killer.kills == 1 and killer.score == 100
    assert server.teams[TEAM1].score == 1
    assert mode.ended and mode.winner == TEAM1
    assert plugin_calls == ["on_test_backlog"] * backlog + ["on_player_death", "on_player_kill"]
    assert not server._mode_events


def test_headshot_awards_bonus():
    server, mode = _new_match()
    killer = _make_player(server, 0, "Killer", TEAM1)
    victim = _make_player(server, 1, "Victim", TEAM2)

    victim.damage(999, source=killer, kill_type=KILL_HEADSHOT)
    asyncio.run(server.drain())

    assert server.teams[TEAM1].score == mode.kill_points + mode.headshot_bonus
    assert server.teams[TEAM1].score == 1
    assert killer.score == 150


def test_timeout_finishes_its_fixed_event_batch_despite_continuing_arrivals():
    from server.simulation_runtime import SimulationRuntime

    server, mode = _new_match()
    killer = _make_player(server, 0, "Killer", TEAM1)
    victim = _make_player(server, 1, "Victim", TEAM2)
    late_killer = _make_player(server, 2, "LateKiller", TEAM2)
    mode.time_limit = 1
    mode.start_time = 0.0
    mode._timeout_music_played = True
    server.loop_count = 1
    server._mode_events = deque(("on_test_backlog", ()) for _ in range(2))
    server.config.mode_event_drain_budget = 1

    async def no_op(*_args):
        pass

    server.plugin_manager = SimpleNamespace(call_event=no_op)
    mode._run_end_sequence = no_op
    victim.damage(999, source=killer)
    runtime = SimulationRuntime(server)

    async def run():
        await runtime._tick_mode()
        assert mode._timeout_events_remaining == 3
        # More callbacks arrive than the drain can process. A live-queue-empty
        # guard never finishes here, and an unrestricted final batch can give
        # these later kills points before choosing the timeout winner.
        for remaining in (2, 1, 0):
            for _ in range(2):
                server.queue_mode_event("on_player_kill", late_killer, killer, 0)
            await runtime._tick_mode()
            assert mode._timeout_events_remaining == remaining
            assert mode.ended == (remaining == 0)
        assert len(server._mode_events) == 6
        assert killer.score == 100 and late_killer.score == 0
        assert mode.winner == TEAM1
        # The next round must not inherit the prior cutoff and skip its queue.
        server._mode_events.clear()
        await mode.on_mode_start()
        assert mode._timeout_events_remaining is None and not mode.ended

    asyncio.run(run())


def test_team_change_death_does_not_score():
    """A team-change death has killer=None -> on_player_death fires but
    on_player_kill never does, so no team scores."""
    server, mode = _new_match()
    victim = _make_player(server, 0, "Quitter", TEAM1)

    victim.die(killer=None, kill_type=KILL_TEAM_CHANGE)
    # on_player_kill must NOT have been queued.
    assert not any(name == "on_player_kill" for name, _ in server._mode_events)
    assert any(name == "on_player_death" for name, _ in server._mode_events)
    asyncio.run(server.drain())
    assert server.teams[TEAM1].score == 0
    assert server.teams[TEAM2].score == 0


def test_friendly_fire_does_not_score():
    """Same-team killer must not credit team score (defense-in-depth guard)."""
    server, mode = _new_match()
    killer = _make_player(server, 0, "A", TEAM1)
    victim = _make_player(server, 1, "B", TEAM1)

    victim.damage(999, source=killer, kill_type=0)
    assert not any(name == "on_player_kill" for name, _ in server._mode_events)
    asyncio.run(server.drain())
    assert server.teams[TEAM1].score == 0
    assert killer.score == -100
    assert killer.kills == 0 and killer.kill_streak == 0


def test_melee_scores_retail_amount_and_duplicate_death_is_ignored():
    server, mode = _new_match()
    killer = _make_player(server, 0, "Killer", TEAM1)
    victim = _make_player(server, 1, "Victim", TEAM2)
    victim.damage(999, source=killer, kill_type=C.MELEE_KILL)
    victim.die(killer, C.MELEE_KILL)
    asyncio.run(server.drain())
    assert killer.score == 150 and killer.kills == 1
    assert server.teams[TEAM1].score == 1 and victim.deaths == 1


def test_damage_pipeline_assist_pays_once_and_respawn_clears_old_credit():
    server, mode = _new_match()
    assistant = _make_player(server, 0, "Assistant", TEAM1)
    killer = _make_player(server, 1, "Killer", TEAM1)
    victim = _make_player(server, 2, "Victim", TEAM2)
    victim.damage(50, source=assistant)
    victim.damage(999, source=killer)
    victim.die(killer)
    asyncio.run(server.drain())
    assert assistant.score == 50 and killer.score == 100
    assert server.teams[TEAM1].score == 1
    victim.spawn(100.5, 100.5, 60.0)
    assert not victim.damage_contributions
    victim.damage(999, source=killer)
    asyncio.run(server.drain())
    assert assistant.score == 50 and killer.score == 200


def test_transition_death_does_not_increment_deaths_or_apply_penalty():
    server, mode = _new_match()
    player = _make_player(server, 0, "Player", TEAM1)
    player.die(kill_type=C.CLASS_CHANGE_KILL)
    asyncio.run(server.drain())
    assert player.score == 0 and player.deaths == 0


def test_reaching_score_limit_ends_match():
    server, mode = _new_match()
    mode.score_limit = 3
    killer = _make_player(server, 0, "Killer", TEAM1)

    for i in range(3):
        victim = _make_player(server, 10 + i, f"V{i}", TEAM2)
        victim.damage(999, source=killer, kill_type=0)
        asyncio.run(server.drain())

    assert server.teams[TEAM1].score == 3
    assert mode.ended is True
    assert mode.winner == TEAM1
