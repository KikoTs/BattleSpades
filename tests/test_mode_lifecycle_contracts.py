"""Mode lifecycle contracts shared by every ruleset.

Covers generic retail kill scoring in BaseMode, leave-before-PlayerLeft on
disconnect, dead-on-join when a mode forbids respawn, mode alias
canonicalization, failed-transition recovery, default_mode validation, join
class restrictions, isolated-runtime command refusal, join auto-balance, the
A2S score limit, leave events surviving a restart, and _end_by_time with an
unusual team count.
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

import shared.constants as C
import shared.constants_gamemode as CG
from shared.bytes import ByteReader
from shared.packet import CreatePlayer, KillAction, NewPlayerConnection, PlayerLeft

from modes.base_mode import BaseMode
from modes.tdm import TDMMode
from server.config import ServerConfig, load_config, resolve_mode_code
from server.game_constants import (
    KILL_CLASS_CHANGE,
    KILL_HEADSHOT,
    KILL_MELEE,
    TEAM1,
    TEAM2,
    TEAM_SPECTATOR,
)
from server.main import BattleSpadesServer
from server.match import MatchTransitionService
from server.player import Player
from server.team import Team
from tests.test_reversed_spawn_handshake import DummyServer, make_connection


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self):
        self.server = None
        self.player = None
        self.in_game = True
        self.sent = []
        self.reserved_player_id = None

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))

    def on_disconnect(self):
        pass


class _PlainMode(BaseMode):
    name = "Plain"


def _scoring_server():
    server = SimpleNamespace(
        config=ServerConfig(),
        teams={
            TEAM1: Team(TEAM1, "TEAM1_COLOR", (0, 0, 255)),
            TEAM2: Team(TEAM2, "TEAM2_COLOR", (0, 255, 0)),
        },
        players={},
        connections={},
        broadcast_packets=[],
    )
    server.broadcast = lambda data, **_k: server.broadcast_packets.append(bytes(data))
    return server


def _player(pid, team, server=None):
    player = Player(pid, f"P{pid}", team, C.RIFLE_TOOL, _Conn())
    if server is not None:
        # Generic scores go only to bodies that still own their roster slot.
        server.players[pid] = player
    return player


# ---------------------------------------------------------------------------
# A. generic per-kill scoring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kill_type, expected",
    [
        (0, CG.GENERIC_SCORE_KILL),
        (KILL_HEADSHOT, CG.GENERIC_SCORE_HEADSHOT),
        (KILL_MELEE, CG.GENERIC_SCORE_MELEE),
    ],
)
def test_base_mode_awards_generic_kill_score(kill_type, expected):
    mode = _PlainMode(_scoring_server())
    killer, victim = _player(0, TEAM1, mode.server), _player(1, TEAM2, mode.server)
    asyncio.run(mode.on_player_kill(killer, victim, kill_type))
    assert killer.score == expected == (100 if kill_type == 0 else 150)


def test_base_mode_suicide_and_teamkill_penalties_apply_once():
    mode = _PlainMode(_scoring_server())
    a, b = _player(0, TEAM1, mode.server), _player(1, TEAM1, mode.server)
    # A fall death has no killing player but is self-inflicted.
    asyncio.run(mode.on_player_death(a, None, int(C.FALL_KILL)))
    assert a.score == CG.GENERIC_SCORE_SUICIDE == -100
    asyncio.run(mode.on_player_death(b, a, 0))
    assert a.score == -200
    assert b.score == 0
    # A cross-team death is scored by on_player_kill, never here.
    enemy = _player(2, TEAM2, mode.server)
    asyncio.run(mode.on_player_death(b, enemy, 0))
    assert enemy.score == 0


def test_generic_scoring_skips_ended_spectator_and_class_change():
    mode = _PlainMode(_scoring_server())
    a, b = _player(0, TEAM1), _player(1, TEAM2)
    asyncio.run(mode.on_player_death(a, None, KILL_CLASS_CHANGE))
    assert a.score == 0
    spectator = _player(3, TEAM_SPECTATOR)
    asyncio.run(mode.on_player_kill(spectator, b, 0))
    assert spectator.score == 0
    mode.ended = True
    asyncio.run(mode.on_player_kill(a, b, 0))
    asyncio.run(mode.on_player_death(a, None, 0))
    assert a.score == 0


def test_tdm_awards_personal_kill_score_exactly_once():
    server = _scoring_server()
    server.config.default_mode = "tdm"
    mode = TDMMode(server)
    killer, victim = _player(0, TEAM1, server), _player(1, TEAM2, server)
    asyncio.run(mode.on_player_kill(killer, victim, KILL_HEADSHOT))
    assert killer.score == CG.GENERIC_SCORE_HEADSHOT
    assert server.teams[TEAM1].score == mode.kill_points
    asyncio.run(mode.on_player_death(victim, killer, KILL_HEADSHOT))
    assert killer.score == CG.GENERIC_SCORE_HEADSHOT


# ---------------------------------------------------------------------------
# B. leave hook before PlayerLeft
# ---------------------------------------------------------------------------


def test_disconnect_runs_mode_leave_before_player_left():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    conn = _Conn()
    player = Player(0, "Leaver", TEAM1, C.RIFLE_TOOL, conn)
    conn.player = player
    player.spawn(100.5, 100.5, 59.75)
    server.players[0] = player
    server.teams[TEAM1].add_player(player)
    peer = object()
    server.connections[peer] = conn
    order = []
    server.broadcast = lambda data, **_k: order.append(("packet", bytes(data)[0]))

    class _LeaveMode(_PlainMode):
        async def on_player_leave(self, left):
            # Still a valid roster entry while mode drop packets go out.
            assert server.players.get(left.id) is left
            assert left in server.teams[TEAM1].players
            order.append(("leave", left.id))
            server.broadcast(b"\x10drop")

    server.mode = _LeaveMode(server)
    # A late event for the departing id must not run after its leave.
    server.queue_mode_event("on_player_death", player, None, 0)
    server.queue_mode_event("on_player_spawn", _player(5, TEAM2))

    server._on_disconnect_sync(peer)

    assert order[0] == ("leave", 0)
    assert order.index(("packet", 0x10)) < order.index(("packet", PlayerLeft.id))
    assert 0 not in server.players
    assert [name for name, _ in server._mode_events] == ["on_player_spawn"]


def test_suspending_leave_hook_finishes_on_the_event_loop():
    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        seen = []

        async def hook(player):
            seen.append("start")
            await asyncio.sleep(0)
            seen.append("end")

        server._drive_hook_now("test", hook, None)
        assert seen == ["start"]
        await asyncio.gather(*server._connection_tasks)
        return seen

    assert asyncio.run(scenario()) == ["start", "end"]


# ---------------------------------------------------------------------------
# C. dead on join
# ---------------------------------------------------------------------------


def _join(team=TEAM1, class_id=0, name="Joiner"):
    packet = NewPlayerConnection()
    packet.team = team
    packet.class_id = class_id
    packet.forced_team = 0
    packet.local_language = 0
    packet.name = name
    return packet


def test_joiner_starts_dead_when_mode_forbids_respawn():
    server = DummyServer()
    allow = {"value": False}
    server.mode = SimpleNamespace(
        can_player_respawn=lambda _p: allow["value"],
        respawn_time_for=lambda _p: 7,
        on_player_join=lambda _p: asyncio.sleep(0),
    )
    connection = make_connection(server)
    sent = []
    connection.send = lambda data, reliable=True, prefix=0x30: sent.append(bytes(data))

    asyncio.run(connection._on_new_player(_join()))

    player = connection.player
    assert player.alive is False and player.spawned is False
    assert player.death_time > 0.0
    ids = [packet[0] for packet in sent]
    assert ids.index(CreatePlayer.id) < ids.index(KillAction.id)
    death = KillAction(ByteReader(sent[ids.index(KillAction.id)][1:]))
    assert death.player_id == player.id and death.respawn_time == 7
    assert all(packet[0] not in (CreatePlayer.id, 69) for packet in server.broadcast_packets)
    assert 69 not in ids  # no Restock/HP for a life that does not exist

    # The ordinary respawn path creates the first life once allowed.
    from server.round_lifecycle import RoundLifecycle

    allow["value"] = True
    server.mode.respawn_time_for = lambda _p: 0
    respawned = []
    lifecycle = RoundLifecycle(server)
    lifecycle.respawn_player = lambda p: respawned.append(p)
    asyncio.run(lifecycle.process_respawns())
    assert respawned == [player]


def test_joiner_spawns_normally_when_mode_allows():
    server = DummyServer()
    server.mode = SimpleNamespace(
        can_player_respawn=lambda _p: True,
        on_player_join=lambda _p: asyncio.sleep(0),
    )
    connection = make_connection(server)
    connection.send = lambda *a, **k: None
    asyncio.run(connection._on_new_player(_join()))
    assert connection.player.alive is True
    assert any(packet[0] == CreatePlayer.id for packet in server.broadcast_packets)


# ---------------------------------------------------------------------------
# 1. mode aliases
# ---------------------------------------------------------------------------


def test_mode_aliases_canonicalize_to_short_codes():
    from modes import canonical_mode_code

    assert canonical_mode_code("occupation") == "oc"
    assert canonical_mode_code("Diamond") == "dia"
    assert canonical_mode_code("classic_ctf") == "cctf"
    assert canonical_mode_code("zombie") == "zom"
    assert canonical_mode_code("arena") == "arena"
    assert canonical_mode_code("nonsense") is None


def test_map_metadata_honours_alias_mode(tmp_path):
    import json
    from server.map_metadata import _mode_applies, canonical_mode_code

    assert canonical_mode_code("occupation") == "oc"
    assert _mode_applies("oc", "occupation")
    assert _mode_applies("occupation", "oc")
    assert not _mode_applies("dia", "occupation")


def test_mode_change_already_active_is_alias_aware():
    server = SimpleNamespace(
        config=SimpleNamespace(default_mode="oc", default_map="London"),
        connections={},
    )
    service = MatchTransitionService(server)
    result = service.request_mode_change("occupation")
    assert result.ok and "already active" in result.message
    assert service._request_task is None


def test_mode_change_stores_the_canonical_code(monkeypatch):
    from tests.test_match_transitions import _NewMode, _Server

    server = _Server()
    service = MatchTransitionService(server)
    seen = []
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _n: _NewMode)
    monkeypatch.setattr(
        service,
        "_load_world_candidate",
        lambda map_name, mode_name: seen.append(mode_name)
        or SimpleNamespace(map_name=map_name, config=None),
    )
    result = asyncio.run(service.change_mode("diamond"))
    assert result.ok
    assert seen == ["dia"]
    assert server.config.default_mode == "dia"


# ---------------------------------------------------------------------------
# 2. failed transition / end sequence recovery
# ---------------------------------------------------------------------------


def test_failed_rollover_restarts_old_mode_and_rebinds_bots(monkeypatch):
    from tests.test_match_transitions import _Server

    class _Old:
        def __init__(self):
            self.starts = 0
            self.deactivated = 0

        async def cancel_end_sequence(self):
            pass

        async def deactivate(self):
            self.deactivated += 1

        async def on_mode_start(self):
            self.starts += 1

    class _Broken:
        def __init__(self, _server):
            pass

        async def on_mode_start(self):
            raise RuntimeError("boom")

        async def deactivate(self):
            pass

    server = _Server()
    old = _Old()
    server.mode = old
    rebinds = []
    server.bots = SimpleNamespace(
        prepare_for_game_transition=lambda: asyncio.sleep(0),
        rebind_after_match_transition=lambda: rebinds.append(1),
    )
    service = MatchTransitionService(server)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _n: _Broken)
    monkeypatch.setattr(
        service,
        "_load_world_candidate",
        lambda m, n: SimpleNamespace(map_name=m, config=None),
    )
    result = asyncio.run(service.change_mode("tdm"))
    assert result.ok is False
    assert server.mode is old
    assert old.starts == 1
    assert rebinds == [1]
    assert server.config.default_mode == "ctf"


def test_failed_end_sequence_falls_back_to_in_place_restart():
    server = _scoring_server()
    server.match_transition = SimpleNamespace(
        _transition_busy=lambda **_k: False,
    )
    mode = _PlainMode(server)
    server.mode = mode
    mode.ended = True
    restarts = []

    async def restart():
        restarts.append(1)
        mode.ended = False

    mode._restart_round = restart
    asyncio.run(mode._recover_failed_end_sequence())
    assert restarts == [1]

    # Another transition owning the epoch keeps the fallback away.
    mode.ended = True
    server.match_transition = SimpleNamespace(_transition_busy=lambda **_k: True)
    asyncio.run(mode._recover_failed_end_sequence())
    assert restarts == [1]


# ---------------------------------------------------------------------------
# 3. default_mode validation
# ---------------------------------------------------------------------------


def test_invalid_default_mode_fails_fast(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[game]\ndefault_mode = "tmd"\n', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert "tdm" in str(error.value)


def test_default_mode_alias_is_canonicalized(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[game]\ndefault_mode = "zombie"\n', encoding="utf-8")
    assert load_config(path).default_mode == "zom"
    assert resolve_mode_code("Occupation") == "oc"


def test_start_rejects_unregistered_mode():
    server = BattleSpadesServer(ServerConfig(default_mode="nope"))
    with pytest.raises(ValueError):
        asyncio.run(server.start())


# ---------------------------------------------------------------------------
# 4. class restrictions
# ---------------------------------------------------------------------------


def test_join_with_disabled_class_falls_back_to_an_allowed_class():
    from server.game_rules import GameRules

    server = DummyServer()
    server.config.game_rules = GameRules.server_defaults()
    server.config.game_rules.apply({"RULE_ENABLE_CLASS_MINER": False})
    connection = make_connection(server)
    connection.send = lambda *a, **k: None
    asyncio.run(connection._on_new_player(_join(class_id=int(C.CLASS_MINER))))
    assert connection.player.class_id != int(C.CLASS_MINER)
    assert connection.player.class_id == int(C.CLASS_SOLDIER)


def test_join_with_mode_foreign_class_falls_back():
    server = DummyServer()
    server.config.default_mode = "tdm"
    server.config.game_mode = "tdm"
    connection = make_connection(server)
    connection.send = lambda *a, **k: None
    asyncio.run(connection._on_new_player(_join(class_id=int(C.CLASS_ZOMBIE))))
    assert connection.player.class_id != int(C.CLASS_ZOMBIE)


def test_in_game_class_change_rejects_mode_foreign_class():
    from server.handlers.equipment import handle_change_class

    server = BattleSpadesServer(ServerConfig(default_mode="tdm"))
    server.mode = None
    player = _player(0, TEAM1)
    player.class_id = int(C.CLASS_SOLDIER)
    asyncio.run(handle_change_class(
        server, player, SimpleNamespace(class_id=int(C.CLASS_ZOMBIE))
    ))
    assert getattr(player, "pending_selection", None) is None


# ---------------------------------------------------------------------------
# 5. isolated runtimes refuse lifecycle commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["ugc_runtime", "tutorial_runtime"])
@pytest.mark.parametrize("command", ["restart", "endround", "mode", "map"])
def test_isolated_runtimes_refuse_lifecycle_commands(flag, command, monkeypatch):
    import commands.server_commands as sc

    messages = []

    async def send_message(_server, _player, text):
        messages.append(text)

    monkeypatch.setattr(sc, "send_message", send_message)

    class _Guard:
        def __getattr__(self, name):
            raise AssertionError(f"transition touched: {name}")

    server = SimpleNamespace(
        config=SimpleNamespace(**{flag: True}),
        match_transition=_Guard(),
        mode=SimpleNamespace(ended=False),
    )
    ctx = SimpleNamespace(server=server, player=None, args=["tdm"], raw_args="tdm")
    handler = getattr(sc, f"cmd_{command}")
    asyncio.run(handler(ctx))
    assert messages and "Not available" in messages[0]


# ---------------------------------------------------------------------------
# 6. auto balance + A2S limit
# ---------------------------------------------------------------------------


def _balanced_server(auto_balance=True, threshold=2):
    server = DummyServer()
    server.config.auto_balance = auto_balance
    server.config.balance_threshold = threshold
    for pid in range(3):
        server.players[pid] = SimpleNamespace(team=TEAM1)
    return server


def test_join_auto_balance_moves_joiner_to_smaller_team():
    server = _balanced_server()
    connection = make_connection(server)
    assert connection._resolve_join_team(TEAM1) == (TEAM2, TEAM2)
    assert connection._resolve_join_team(TEAM2) == (TEAM2, TEAM2)
    # Balancing off, below threshold, or host-assigned: keep the request.
    assert make_connection(_balanced_server(False))._resolve_join_team(TEAM1)[0] == TEAM1
    assert make_connection(_balanced_server(threshold=4))._resolve_join_team(TEAM1)[0] == TEAM1
    assert connection._resolve_join_team(TEAM1, balance=False)[0] == TEAM1


def test_join_auto_balance_respects_mode_team_assignment():
    server = _balanced_server()
    server.mode = SimpleNamespace(prepare_join_team=lambda team: TEAM1)
    assert make_connection(server)._resolve_join_team(TEAM1)[0] == TEAM1


def test_a2s_rules_report_active_mode_score_limit():
    import struct

    server = BattleSpadesServer(ServerConfig(default_mode="tdm"))
    server.mode = SimpleNamespace(score_limit=200)
    data = server.a2s_handler._make_rules_response()
    assert b"score_limit\x00200\x00" in data


# ---------------------------------------------------------------------------
# 7. restart keeps pending leave events
# ---------------------------------------------------------------------------


def test_restart_round_runs_pending_leave_before_discarding_queue():
    from tests.test_match_transitions import _Server

    server = _Server()
    left = []

    async def on_player_leave(player):
        left.append(player)

    server.mode.on_player_leave = on_player_leave
    departed = object()
    server._mode_events = deque([
        ("on_player_leave", (departed,)),
        ("on_block_build", (object(), 1, 2, 3)),
    ])
    result = asyncio.run(MatchTransitionService(server).restart_round())
    assert result.ok
    assert left == [departed]
    assert list(server._mode_events) == []


# ---------------------------------------------------------------------------
# 8. _end_by_time with unusual team counts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("teams, winner", [
    ({}, None),
    ({TEAM1: 3}, TEAM1),
    ({TEAM1: 2, TEAM2: 2}, None),
])
def test_end_by_time_handles_any_team_count(teams, winner):
    server = _scoring_server()
    server.teams = {}
    for team_id, score in teams.items():
        team = Team(team_id, "TEAM1_COLOR", (0, 0, 0))
        team.score = score
        server.teams[team_id] = team
    mode = _PlainMode(server)
    ended = []

    async def on_mode_end(result):
        ended.append(result)

    mode.on_mode_end = on_mode_end
    asyncio.run(mode._end_by_time())
    assert ended == [winner]


# ---------------------------------------------------------------------------
# 10. extra: alias overlay tables, zero score limit
# ---------------------------------------------------------------------------


def test_alias_mode_overlay_table_reaches_the_short_code(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[modes.zombie]\ntime_limit = 111\ninfection_delay = 3\n"
        "[modes.zom]\ntime_limit = 222\n",
        encoding="utf-8",
    )
    settings = load_config(path).mode_settings
    assert settings["zom"] == {"time_limit": 222, "infection_delay": 3}
    assert "zombie" not in settings


def test_zero_score_limit_never_wins():
    server = _scoring_server()
    mode = _PlainMode(server)
    mode.score_limit = 0
    assert asyncio.run(mode.check_win_condition()) is None


def test_dead_joiner_respawned_while_gated_gets_its_own_create_player():
    from server.roster import player_life_token

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    conn = _Conn()
    conn.server = server
    player = Player(0, "Late", TEAM1, C.RIFLE_TOOL, conn)
    conn.player = player
    conn.join_death_token = player_life_token(player)
    player.spawn(100.5, 100.5, 59.75)  # life began before first ClientData
    server._repair_dead_join_respawn(conn)
    assert conn.sent and conn.sent[0][0] == CreatePlayer.id
    assert conn.join_death_token is None

    # Still dead: nothing is sent.
    conn2 = _Conn()
    player2 = Player(1, "Waiting", TEAM1, C.RIFLE_TOOL, conn2)
    conn2.player = player2
    conn2.join_death_token = player_life_token(player2)
    server._repair_dead_join_respawn(conn2)
    assert conn2.sent == []
