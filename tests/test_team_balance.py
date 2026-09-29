"""Mid-match auto-balance (server/team_balance.py)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import ChatMessage, LocalisedMessage

from server.game_constants import TEAM1, TEAM2
from server.handlers import team as team_handlers
from server.main import BattleSpadesServer
from server.team import Team
from server.team_balance import TeamBalancer, team_counts


class _Player:
    def __init__(self, player_id, team, *, alive=False, bot=False, score=0):
        self.id = player_id
        self.name = f"P{player_id}"
        self.team = team
        self.alive = alive
        self.is_bot = bot
        self.score = score
        self.captures = 0
        self.pickup_id = None
        self.death_time = 100.0 if not alive else 0.0
        self.connection = SimpleNamespace(in_game=True)
        self.sent = []

    def send(self, data, reliable=True):
        self.sent.append(bytes(data))

    def die(self, **_kwargs):
        self.alive = False


class _Director:
    def __init__(self, server):
        self.server = server
        self.bots = []
        self._started = True
        self._reconnect_count = None
        self.removed = []
        self.added = []

    def _safe_to_retire(self, bot):
        return bot.pickup_id is None

    async def remove_bot(self, bot, *, force=False):
        self.bots.remove(bot)
        self.server.teams[bot.team].remove_player(bot)
        self.server.players.pop(bot.id)
        self.removed.append(bot)
        return True

    async def add_bot(self, team=None, **_kwargs):
        new_id = max(self.server.players, default=0) + 1
        bot = _Player(new_id, team, alive=True, bot=True)
        _add(self.server, bot)
        self.bots.append(bot)
        self.added.append(bot)
        return bot


def _server(*, threshold=2, mode=None, grace=0.0, **config):
    teams = {
        TEAM1: Team(TEAM1, "TEAM1_COLOR", (0, 0, 255)),
        TEAM2: Team(TEAM2, "TEAM2_COLOR", (0, 255, 0)),
    }
    server = SimpleNamespace(
        config=SimpleNamespace(
            auto_balance=True,
            balance_threshold=threshold,
            balance_grace_seconds=grace,
            **config,
        ),
        teams=teams,
        players={},
        connections={},
        mode=mode if mode is not None else SimpleNamespace(started=True, ended=False),
        events=[],
        bots=None,
    )
    server.queue_mode_event = lambda name, *args: server.events.append((name, args))
    return server


def _add(server, player):
    server.players[player.id] = player
    server.teams[player.team].add_player(player)
    return player


def _tick(balancer, now):
    return asyncio.run(balancer.tick(now))


def _populate(server, big=4, small=1, *, alive=False, start_id=0):
    players = []
    for index in range(big):
        players.append(_add(server, _Player(start_id + index, TEAM1, alive=alive)))
    for index in range(small):
        players.append(_add(server, _Player(start_id + 100 + index, TEAM2, alive=alive)))
    return players


def test_even_or_one_ahead_teams_are_left_alone():
    server = _server(threshold=1)
    _populate(server, big=3, small=2)
    balancer = TeamBalancer(server)
    # threshold 1 is raised to 2: a one-player lead cannot be improved.
    assert not _tick(balancer, 10.0)
    assert team_counts(server) == {TEAM1: 3, TEAM2: 2}


def test_moves_a_dead_human_from_the_bigger_team_with_retail_notice():
    server = _server()
    players = _populate(server, big=4, small=1)
    balancer = TeamBalancer(server)
    assert _tick(balancer, 10.0)
    assert team_counts(server) == {TEAM1: 3, TEAM2: 2}
    moved = [player for player in players[:4] if player.team == TEAM2]
    assert len(moved) == 1
    mover = moved[0]
    assert mover in server.teams[TEAM2].players
    assert mover.death_time == 100.0  # respawns on schedule, on the new side
    assert server.events[-1][0] == "on_player_team_change"
    overlay = LocalisedMessage(ByteReader(mover.sent[0][1:]))
    assert overlay.string_id == "TEAM_FULL"
    chat = ChatMessage(ByteReader(mover.sent[1][1:]))
    assert chat.chat_type == int(C.CHAT_SYSTEM)
    assert "other team" in chat.value


def test_grace_period_waits_before_acting():
    server = _server(grace=5.0)
    _populate(server, big=4, small=1)
    balancer = TeamBalancer(server)
    assert not _tick(balancer, 10.0)
    assert not _tick(balancer, 12.0)
    assert _tick(balancer, 15.5)


def test_live_players_are_never_killed_to_balance():
    server = _server()
    _populate(server, big=4, small=1, alive=True)
    balancer = TeamBalancer(server)
    for second in range(10, 20):
        assert not _tick(balancer, float(second))
    assert team_counts(server) == {TEAM1: 4, TEAM2: 1}
    # Once one of them dies they move at the next check.
    server.players[2].alive = False
    assert _tick(balancer, 21.0)
    assert server.players[2].team == TEAM2


def test_carriers_vips_and_mode_locks_are_skipped():
    vip = _Player(0, TEAM1)
    carrier = _Player(1, TEAM1)
    carrier.pickup_id = 3
    locked = _Player(2, TEAM1)
    free = _Player(3, TEAM1)
    mode = SimpleNamespace(
        started=True,
        ended=False,
        vips={TEAM1: vip},
        allows_team_change=lambda player, team: player is not locked,
    )
    server = _server(mode=mode)
    for player in (vip, carrier, locked, free):
        _add(server, player)
    _add(server, _Player(100, TEAM2))
    balancer = TeamBalancer(server)
    assert _tick(balancer, 10.0)
    assert free.team == TEAM2
    assert vip.team == carrier.team == locked.team == TEAM1


def test_mode_assigned_teams_and_ended_rounds_are_not_balanced():
    for mode in (
        SimpleNamespace(started=True, ended=False, prepare_join_team=lambda t: t),
        SimpleNamespace(started=True, ended=True),
        SimpleNamespace(started=True, ended=False, auto_balance_enabled=False),
    ):
        server = _server(mode=mode)
        _populate(server, big=4, small=1)
        assert not _tick(TeamBalancer(server), 10.0)
        assert team_counts(server) == {TEAM1: 4, TEAM2: 1}


def test_auto_balance_off_or_mid_match_off_disables_it():
    server = _server()
    server.config.auto_balance = False
    _populate(server, big=4, small=1)
    assert not _tick(TeamBalancer(server), 10.0)
    server.config.auto_balance = True
    server.config.balance_mid_match = False
    assert not _tick(TeamBalancer(server), 10.0)


def test_same_player_is_not_bounced_twice_within_the_cooldown():
    server = _server(balance_player_cooldown=600.0)
    only = _add(server, _Player(0, TEAM1))
    for index in range(1, 4):
        _add(server, _Player(index, TEAM1, alive=True))
    _add(server, _Player(100, TEAM2, alive=True))
    balancer = TeamBalancer(server)
    assert _tick(balancer, 10.0)
    assert only.team == TEAM2
    # Now make TEAM2 the big side with only the recently moved player dead.
    for index in (101, 102, 103):
        _add(server, _Player(index, TEAM2, alive=True))
    server.teams[TEAM1].remove_player(server.players[1])
    server.players.pop(1)
    assert team_counts(server) == {TEAM1: 2, TEAM2: 5}
    for second in range(11, 30):
        assert not _tick(balancer, float(second))
    assert only.team == TEAM2
    assert _tick(balancer, 700.0)  # cooldown over
    assert only.team == TEAM1


def test_lowest_impact_newest_human_is_chosen():
    server = _server()
    _add(server, _Player(100, TEAM2, alive=True))
    veteran = _add(server, _Player(0, TEAM1, score=40))
    balancer = TeamBalancer(server)
    asyncio.run(balancer.tick(1.0))  # veteran seen first
    early = _add(server, _Player(1, TEAM1, score=5))
    asyncio.run(balancer.tick(2.0))
    late = _add(server, _Player(2, TEAM1, score=5))
    _add(server, _Player(3, TEAM1, alive=True))
    assert _tick(balancer, 3.0)
    assert late.team == TEAM2
    assert early.team == veteran.team == TEAM1


def test_forced_move_ignores_player_cooldown_but_not_mode_lock():
    server = _server()
    player = _add(server, _Player(0, TEAM1))
    player._last_team_change_at = 10**9  # just switched voluntarily
    assert team_handlers.change_team(server, player, TEAM2, force=True)
    assert player.team == TEAM2
    server.mode = SimpleNamespace(allows_team_change=lambda *_: False)
    assert not team_handlers.change_team(server, player, TEAM1, force=True)
    assert player.team == TEAM2


def test_bots_even_the_teams_before_any_human_moves():
    server = _server()
    director = server.bots = _Director(server)
    humans = [_add(server, _Player(index, TEAM1)) for index in range(2)]
    for index in range(10, 13):
        bot = _add(server, _Player(index, TEAM1, bot=True))
        director.bots.append(bot)
    _add(server, _Player(100, TEAM2))
    balancer = TeamBalancer(server)
    assert _tick(balancer, 10.0)
    assert all(human.team == TEAM1 for human in humans)
    assert sum(1 for bot in director.bots if bot.team == TEAM2) == 1
    assert _tick(balancer, 11.0)
    assert team_counts(server) == {TEAM1: 3, TEAM2: 3}
    assert all(human.team == TEAM1 for human in humans)
    assert not _tick(balancer, 12.0)


def test_live_bots_are_retired_and_replaced_after_the_bot_wait():
    server = _server(balance_bot_wait_seconds=10.0)
    director = server.bots = _Director(server)
    for index in range(10, 14):
        bot = _add(server, _Player(index, TEAM1, alive=True, bot=True))
        director.bots.append(bot)
    human = _add(server, _Player(0, TEAM1))
    _add(server, _Player(100, TEAM2, alive=True))
    balancer = TeamBalancer(server)
    # A live bot on the big side will die soon: nobody moves yet, and the
    # dead human is left alone while a bot can still fix it.
    assert not _tick(balancer, 10.0)
    assert not _tick(balancer, 15.0)
    assert human.team == TEAM1
    assert _tick(balancer, 20.5)
    assert len(director.removed) == 1 and director.removed[0].team == TEAM1
    assert len(director.added) == 1 and director.added[0].team == TEAM2
    assert team_counts(server) == {TEAM1: 4, TEAM2: 2}
    assert human.team == TEAM1


def test_bot_roster_in_transition_is_untouched():
    server = _server()
    director = server.bots = _Director(server)
    director._reconnect_count = 3
    bot = _add(server, _Player(10, TEAM1, bot=True))
    director.bots.append(bot)
    for index in range(3):
        _add(server, _Player(index, TEAM1, alive=True))
    _add(server, _Player(100, TEAM2, alive=True))
    assert not _tick(TeamBalancer(server), 10.0)
    assert bot.team == TEAM1


def test_periodic_services_run_once_per_second_and_isolate_failures():
    calls = []

    class _Balancer:
        async def tick(self, now):
            calls.append("balance")
            raise RuntimeError("boom")

    fake = SimpleNamespace(
        tick_rate=60,
        loop_count=59,
        team_balance=_Balancer(),
        bot_skill_balance=SimpleNamespace(update=lambda now: calls.append("skill")),
    )
    asyncio.run(BattleSpadesServer._run_periodic_services(fake))
    assert calls == []
    fake.loop_count = 60
    asyncio.run(BattleSpadesServer._run_periodic_services(fake))
    assert calls == ["balance", "skill"]
