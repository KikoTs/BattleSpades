"""Anti-cheat regressions for commands, team/class transitions and /admin.

Covers:
* transition deaths (/kill, ChangeClass, SetClassLoadout, ChangeTeam, /team)
  right after enemy damage are the attacker's kill, not a free denial;
* the class-change death cooldown (menu picks while dead stay instant);
* /team shares every rule with the ChangeTeam packet;
* /admin: disabled with the default/short/empty password, constant-time
  compare, kick + temporary IP ban after repeated failures;
* the per-player slash-command rate limit (admins exempt);
* /me and /pm respect mute and the chat length limit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

import shared.constants as C
import commands.admin as admin_cmds
import commands.player as player_cmds
from commands.command_handler import CommandContext
from server import anticheat
from server.bans import BanManager
from server.config import (
    ServerConfig,
    admin_password_problem,
    load_config,
)
from server.game_constants import (
    KILL_CLASS_CHANGE,
    KILL_TEAM_CHANGE,
    KILL_WEAPON,
    TEAM1,
    TEAM2,
    TEAM_SPECTATOR,
)
from server.handlers import equipment as equipment_handlers
from server.handlers import social
from server.handlers import team as team_handlers
from server.player import Player
from server.team import Team
from shared.bytes import ByteReader
from shared.packet import ChatMessage, KillAction

STRONG_PASSWORD = "correct horse battery"


# --- fakes -----------------------------------------------------------------


class _Conn:
    def __init__(self, server=None, address=None):
        self.server = server
        self.player = None
        self.in_game = True
        self.sent = []
        self.reserved_player_id = None
        self.disconnected = None
        self.peer = SimpleNamespace(address=address) if address else None

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))

    def disconnect(self, reason=0):
        self.disconnected = reason

    def on_disconnect(self):
        pass


def _server(*, mode=None):
    server = SimpleNamespace(
        config=ServerConfig(),
        teams={
            TEAM1: Team(TEAM1, "TEAM1_COLOR", (0, 0, 255)),
            TEAM2: Team(TEAM2, "TEAM2_COLOR", (0, 255, 0)),
        },
        players={},
        connections={},
        broadcasts=[],
        events=[],
        mode=mode if mode is not None else SimpleNamespace(),
        retired=[],
        world_manager=None,
    )
    server.config.admin_password = STRONG_PASSWORD
    server.broadcast = lambda data, **_k: server.broadcasts.append(bytes(data))
    server.queue_mode_event = lambda name, *args: server.events.append((name, args))
    server.round_lifecycle = SimpleNamespace(
        remove_owned_deployables=lambda player: server.retired.append(player.id)
    )
    return server


def _player(server, pid, team, *, address=None):
    connection = _Conn(server, address)
    player = Player(pid, f"P{pid}", team, C.RIFLE_TOOL, connection)
    connection.player = player
    player.class_id = int(C.CLASS_SOLDIER)
    player.alive = True
    player.spawned = True
    player.spawned_at = time.monotonic() - 30.0
    server.players[pid] = player
    server.teams[team].add_player(player)
    return player


def _hit(victim, attacker, *, ago=0.5):
    victim._last_combat_damage_at = time.monotonic() - ago
    victim._last_damage_source_id = attacker.id
    victim.health = 1


def _kill_actions(server):
    return [
        KillAction(ByteReader(data[1:]))
        for data in server.broadcasts
        if data[0] == KillAction.id
    ]


def _events(server, name):
    return [args for event, args in server.events if event == name]


def _change_class(server, player, class_id):
    asyncio.run(equipment_handlers.handle_change_class(
        server, player, SimpleNamespace(class_id=int(class_id))
    ))


def _set_loadout(server, player, class_id, loadout=()):
    asyncio.run(equipment_handlers.handle_set_class_loadout(
        server,
        player,
        SimpleNamespace(
            class_id=int(class_id), loadout=list(loadout), prefabs=[],
            ugc_tools=[], instant=0,
        ),
    ))


def _ctx(server, player, *args):
    return CommandContext(
        server=server, player=player, args=list(args), raw_args=" ".join(args)
    )


def _system_lines(player):
    lines = []
    for data in player.connection.sent:
        if data[0] == ChatMessage.id:
            lines.append(ChatMessage(ByteReader(data[1:])).value)
    return lines


def _localised_ids(player):
    from shared.packet import LocalisedMessage

    return [
        LocalisedMessage(ByteReader(data[1:])).string_id
        for data in player.connection.sent
        if data[0] == LocalisedMessage.id
    ]


@pytest.fixture
def captured(monkeypatch):
    messages = []

    async def fake_send(server, player, message):
        messages.append((player.name, message))

    monkeypatch.setattr(admin_cmds, "send_message", fake_send)
    monkeypatch.setattr(player_cmds, "send_message", fake_send)
    return messages


# --- 1. transition deaths no longer deny kills -------------------------------


def test_class_change_at_low_hp_after_enemy_fire_is_the_attackers_kill():
    server = _server()
    victim = _player(server, 1, TEAM1)
    attacker = _player(server, 2, TEAM2)
    _hit(victim, attacker)

    _change_class(server, victim, C.CLASS_MINER)

    assert not victim.alive
    assert victim.deaths == 1
    assert attacker.kills == 1
    (action,) = _kill_actions(server)
    assert action.killer_id == attacker.id
    assert action.kill_type == KILL_WEAPON
    assert _events(server, "on_player_kill") == [(attacker, victim, KILL_WEAPON)]
    # The class change itself still happens at the next respawn.
    assert int(victim.pending_selection.class_id) == int(C.CLASS_MINER)
    assert anticheat.summary(victim)["transition_death_credited"] == 1


def test_loadout_change_after_enemy_fire_is_the_attackers_kill():
    server = _server()
    victim = _player(server, 1, TEAM1)
    attacker = _player(server, 2, TEAM2)
    _hit(victim, attacker)

    _set_loadout(server, victim, C.CLASS_MINER)

    assert not victim.alive and victim.deaths == 1 and attacker.kills == 1
    assert _kill_actions(server)[0].killer_id == attacker.id


def test_class_change_without_recent_enemy_damage_stays_a_transition():
    server = _server()
    victim = _player(server, 1, TEAM1)
    _player(server, 2, TEAM2)

    _change_class(server, victim, C.CLASS_MINER)

    assert not victim.alive
    assert victim.deaths == 0
    (action,) = _kill_actions(server)
    assert action.kill_type == KILL_CLASS_CHANGE
    assert _events(server, "on_player_kill") == []


@pytest.mark.parametrize("case", ["stale", "teammate", "previous_life", "left"])
def test_transition_credit_needs_a_fresh_enemy_hit_this_life(case):
    server = _server()
    victim = _player(server, 1, TEAM1)
    enemy = _player(server, 2, TEAM2)
    mate = _player(server, 3, TEAM1)
    if case == "stale":
        _hit(victim, enemy, ago=team_handlers.TRANSITION_DEATH_CREDIT_WINDOW_SECONDS + 1)
    elif case == "teammate":
        _hit(victim, mate)
    elif case == "previous_life":
        _hit(victim, enemy, ago=1.0)
        victim.spawned_at = time.monotonic() - 0.5
    else:
        _hit(victim, enemy)
        del server.players[enemy.id]

    _change_class(server, victim, C.CLASS_MINER)

    assert victim.deaths == 0
    assert enemy.kills == mate.kills == 0
    assert _kill_actions(server)[0].kill_type == KILL_CLASS_CHANGE


def test_class_pick_while_dead_is_instant_and_kills_nobody():
    server = _server()
    victim = _player(server, 1, TEAM1)
    victim.alive = False

    _change_class(server, victim, C.CLASS_MINER)

    assert int(victim.pending_selection.class_id) == int(C.CLASS_MINER)
    assert _kill_actions(server) == []


def test_repeated_class_change_deaths_are_rate_limited(monkeypatch):
    server = _server()
    victim = _player(server, 1, TEAM1)
    clock = [1000.0]
    monkeypatch.setattr(equipment_handlers.time, "monotonic", lambda: clock[0])

    _change_class(server, victim, C.CLASS_MINER)
    assert not victim.alive
    # Respawned quickly (short respawn time) and changes class again.
    victim.alive = True
    victim.spawned = True
    victim.class_id = int(C.CLASS_MINER)
    clock[0] += 1.0
    _change_class(server, victim, C.CLASS_SOLDIER)
    assert victim.alive  # staged, not killed again
    assert int(victim.pending_selection.class_id) == int(C.CLASS_SOLDIER)
    assert any("next respawn" in line for line in _system_lines(victim))
    assert len(_kill_actions(server)) == 1

    clock[0] += equipment_handlers.CLASS_CHANGE_DEATH_COOLDOWN_SECONDS
    _change_class(server, victim, C.CLASS_SOLDIER)
    assert not victim.alive
    assert len(_kill_actions(server)) == 2


def test_kill_command_after_enemy_fire_is_the_attackers_kill(captured):
    server = _server()
    victim = _player(server, 1, TEAM1)
    attacker = _player(server, 2, TEAM2)
    _hit(victim, attacker)

    asyncio.run(player_cmds.cmd_kill(_ctx(server, victim)))

    assert not victim.alive and victim.deaths == 1 and attacker.kills == 1
    assert _events(server, "on_player_kill") == [(attacker, victim, KILL_WEAPON)]


def test_kill_command_without_enemy_fire_is_a_plain_transition(captured):
    server = _server()
    victim = _player(server, 1, TEAM1)

    asyncio.run(player_cmds.cmd_kill(_ctx(server, victim)))

    assert not victim.alive and victim.deaths == 0
    # CLASS_CHANGE_KILL: the stock client resets domination on TEAM_CHANGE.
    assert _kill_actions(server)[0].kill_type == KILL_CLASS_CHANGE


def test_team_change_after_enemy_fire_credits_kill_before_switching_sides():
    server = _server()
    victim = _player(server, 1, TEAM1)
    attacker = _player(server, 2, TEAM2)
    _hit(victim, attacker)

    asyncio.run(team_handlers.handle_change_team(
        server, victim, SimpleNamespace(team=TEAM2)
    ))

    # The move still happens, but the attacker keeps the kill even though
    # the victim is now on the attacker's team.
    assert victim.team == TEAM2
    assert victim.deaths == 1 and attacker.kills == 1
    assert _events(server, "on_player_kill") == [(attacker, victim, KILL_WEAPON)]
    assert len(_kill_actions(server)) == 1
    assert _events(server, "on_player_team_change") == [(victim, TEAM1, TEAM2)]


# --- 2. /team shares the ChangeTeam rules ------------------------------------


def test_team_command_moves_through_shared_rules(captured):
    server = _server()
    mover = _player(server, 1, TEAM1)

    asyncio.run(player_cmds.cmd_team(_ctx(server, mover, "team2")))

    assert mover.team == TEAM2
    assert server.retired == [mover.id]  # deployables retired
    assert _events(server, "on_player_team_change") == [(mover, TEAM1, TEAM2)]
    assert mover._last_team_change_at is not None
    assert captured[-1] == (mover.name, "You joined team 2")


def test_team_command_honours_team_change_cooldown(captured):
    server = _server()
    mover = _player(server, 1, TEAM1)
    mover._last_team_change_at = time.monotonic()

    asyncio.run(player_cmds.cmd_team(_ctx(server, mover, "team2")))

    assert mover.team == TEAM1 and mover.alive
    assert any("change team again" in line for line in _system_lines(mover))
    assert _localised_ids(mover) == ["TEAM_SWITCH_WAIT"]
    assert _events(server, "on_player_team_change") == []


def test_team_command_honours_mode_team_lock(captured):
    mode = SimpleNamespace(allows_team_change=lambda player, team: False)
    server = _server(mode=mode)
    mover = _player(server, 1, TEAM1)

    asyncio.run(player_cmds.cmd_team(_ctx(server, mover, "team2")))

    assert mover.team == TEAM1 and mover.alive
    assert _localised_ids(mover) == ["TEAM_SWITCH_NOT_ALLOWED"]


def test_team_command_honours_auto_balance(captured, monkeypatch):
    server = _server()
    mover = _player(server, 1, TEAM1)
    monkeypatch.setattr(
        team_handlers, "_switch_unbalances_teams", lambda *_a: True
    )

    asyncio.run(player_cmds.cmd_team(_ctx(server, mover, "team2")))

    assert mover.team == TEAM1 and mover.alive
    assert _localised_ids(mover) == ["TEAM_FULL"]


def test_team_command_to_spectator_announces_spectator_roster(captured):
    server = _server()
    mover = _player(server, 1, TEAM1)

    asyncio.run(player_cmds.cmd_team(_ctx(server, mover, "spectator")))

    assert mover.team == TEAM_SPECTATOR
    assert not mover.alive and mover.death_time == 0.0
    from shared.packet import CreatePlayer

    assert any(data[0] == CreatePlayer.id for data in server.broadcasts)


def test_team_command_same_team_is_refused(captured):
    server = _server()
    mover = _player(server, 1, TEAM1)

    asyncio.run(player_cmds.cmd_team(_ctx(server, mover, "team1")))

    assert mover.alive and mover.team == TEAM1
    assert _kill_actions(server) == []


# --- 3. /admin ---------------------------------------------------------------


@pytest.mark.parametrize(
    "password", ["changeme", "", "   ", "short-pass1"]
)
def test_admin_login_disabled_with_default_or_weak_password(captured, password):
    server = _server()
    server.config.admin_password = password
    player = _player(server, 1, TEAM1)

    asyncio.run(admin_cmds.cmd_admin_login(_ctx(server, player, password or "x")))

    assert not player.admin
    assert "disabled" in captured[-1][1]
    assert admin_password_problem(password) is not None


def test_admin_password_problem_accepts_a_long_unique_secret():
    assert admin_password_problem(STRONG_PASSWORD) is None
    assert admin_password_problem("x" * 12) is None


def test_admin_login_accepts_password_with_spaces(captured):
    server = _server()
    player = _player(server, 1, TEAM1)

    asyncio.run(admin_cmds.cmd_admin_login(
        _ctx(server, player, *STRONG_PASSWORD.split())
    ))

    assert player.admin
    assert captured[-1][1] == "You are now an admin."


def test_admin_login_compares_in_constant_time(captured, monkeypatch):
    calls = []
    real = admin_cmds.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(admin_cmds.hmac, "compare_digest", spy)
    server = _server()
    player = _player(server, 1, TEAM1)
    asyncio.run(admin_cmds.cmd_admin_login(_ctx(server, player, "wrong-guess")))
    assert calls == [(b"wrong-guess", STRONG_PASSWORD.encode())]


def test_repeated_failed_admin_logins_kick_and_temp_ban(captured, tmp_path):
    server = _server()
    server.ban_manager = BanManager(str(tmp_path / "bans.json"))
    limit = int(server.config.anticheat.admin_login_attempts)
    first = _player(server, 1, TEAM1, address="203.0.113.9:5555")

    for attempt in range(limit - 1):
        asyncio.run(admin_cmds.cmd_admin_login(_ctx(server, first, f"guess{attempt}")))
    assert first.connection.disconnected is None
    # Reconnecting from the same address does not reset the count.
    second = _player(server, 2, TEAM1, address="203.0.113.9:6000")
    asyncio.run(admin_cmds.cmd_admin_login(_ctx(server, second, "last-guess")))

    assert second.connection.disconnected == int(C.DISCONNECT.ERROR_KICKED)
    assert not second.admin
    ban = server.ban_manager.is_banned("203.0.113.9")
    assert ban is not None
    remaining = ban["until"] - time.time()
    assert admin_cmds.ADMIN_LOGIN_BAN_SECONDS - 5 < remaining <= admin_cmds.ADMIN_LOGIN_BAN_SECONDS
    assert anticheat.summary(second)["admin_login_failed"] == 1


def test_successful_admin_login_resets_failure_count(captured, tmp_path):
    server = _server()
    server.ban_manager = BanManager(str(tmp_path / "bans.json"))
    player = _player(server, 1, TEAM1, address="198.51.100.4:1")
    limit = int(server.config.anticheat.admin_login_attempts)

    for _ in range(limit - 1):
        asyncio.run(admin_cmds.cmd_admin_login(_ctx(server, player, "nope")))
    asyncio.run(admin_cmds.cmd_admin_login(_ctx(server, player, STRONG_PASSWORD)))
    assert player.admin
    other = _player(server, 2, TEAM1, address="198.51.100.4:2")
    asyncio.run(admin_cmds.cmd_admin_login(_ctx(server, other, "nope")))
    assert other.connection.disconnected is None
    assert server.ban_manager.is_banned("198.51.100.4") is None


def _write_config(tmp_path, password_line):
    path = tmp_path / "config.toml"
    path.write_text(f"[admin]\n{password_line}\n", encoding="utf-8")
    return path


def test_load_config_warns_when_admin_login_is_disabled(tmp_path, caplog):
    path = _write_config(tmp_path, 'password = "changeme"')
    with caplog.at_level(logging.WARNING, logger="BattleSpades.config"):
        config = load_config(path)
    assert config.admin_password == "changeme"
    assert "/admin login is DISABLED" in caplog.text


def test_load_config_is_quiet_with_a_strong_admin_password(tmp_path, caplog):
    path = _write_config(tmp_path, f'password = "{STRONG_PASSWORD}"')
    with caplog.at_level(logging.WARNING, logger="BattleSpades.config"):
        config = load_config(path)
    assert config.admin_password == STRONG_PASSWORD
    assert "DISABLED" not in caplog.text


# --- 4. slash-command rate limit ----------------------------------------------


def _slash(server, player, text):
    asyncio.run(social.handle_chat(
        server, player, SimpleNamespace(value=text, chat_type=0)
    ))


def test_slash_commands_are_rate_limited_per_player(monkeypatch):
    server = _server()
    player = _player(server, 1, TEAM1)
    dispatched = []

    async def fake_handle_command(_server, _player, message):
        dispatched.append(message)

    clock = [500.0]
    monkeypatch.setattr("commands.handle_command", fake_handle_command)
    monkeypatch.setattr(social.time, "monotonic", lambda: clock[0])

    for _ in range(int(social.COMMAND_BURST) + 3):
        _slash(server, player, "/admin guess")
    assert len(dispatched) == int(social.COMMAND_BURST)
    notices = [line for line in _system_lines(player) if "too fast" in line]
    assert len(notices) == 1  # one notice, not one per dropped command

    clock[0] += 1.0 / social.COMMAND_REFILL_PER_SECOND
    _slash(server, player, "/help")
    assert dispatched[-1] == "help"


def test_logged_in_admins_are_exempt_from_command_rate_limit(monkeypatch):
    server = _server()
    player = _player(server, 1, TEAM1)
    player.admin = True
    dispatched = []

    async def fake_handle_command(_server, _player, message):
        dispatched.append(message)

    monkeypatch.setattr("commands.handle_command", fake_handle_command)
    for _ in range(20):
        _slash(server, player, "/kick someone")
    assert len(dispatched) == 20


# --- 5. /me and /pm -------------------------------------------------------------


def test_me_respects_mute_and_length(captured):
    server = _server()
    player = _player(server, 1, TEAM1)
    player.muted = True
    asyncio.run(player_cmds.cmd_me(_ctx(server, player, "waves")))
    assert server.broadcasts == []

    player.muted = False
    listener = _player(server, 2, TEAM2)
    listener.connection.known_player_lives = {player.id}
    server.connections = {"a": player.connection, "b": listener.connection}
    asyncio.run(player_cmds.cmd_me(_ctx(server, player, "x" * 500)))
    (line,) = [
        ChatMessage(ByteReader(data[1:]))
        for data in listener.connection.sent
        if data[0] == ChatMessage.id
    ]
    assert len(line.value) <= player_cmds._CHAT_TEXT_LIMIT
    assert line.player_id == player.id


def test_pm_respects_mute(captured):
    server = _server()
    sender = _player(server, 1, TEAM1)
    target = _player(server, 2, TEAM2)
    server.get_player_by_name = lambda name: target
    sender.muted = True

    asyncio.run(player_cmds.cmd_pm(_ctx(server, sender, "P2", "hello")))

    assert captured == [(sender.name, "You are muted.")]


def test_votekick_cooldown_survives_a_reconnect_from_the_same_address():
    from types import SimpleNamespace as _NS
    from server.voting import VoteManager

    first = _NS(id=1, connection=_NS(peer=_NS(address="10.0.0.5:51000")))
    rejoined = _NS(id=7, connection=_NS(peer=_NS(address="10.0.0.5:51377")))
    other = _NS(id=2, connection=_NS(peer=_NS(address="10.0.0.9:51000")))
    assert VoteManager._cooldown_key(first) == VoteManager._cooldown_key(rejoined) == "ip:10.0.0.5"
    assert VoteManager._cooldown_key(other) != VoteManager._cooldown_key(first)
    # Peerless players (bots) keep per-slot cooldowns, never a shared key.
    assert VoteManager._cooldown_key(_NS(id=3, connection=None)) == 3


# --- retail team-change refusals (audit3 server #3, 2026-09-28) -------------


def test_menu_change_team_refusals_use_retail_localised_lines():
    """ChangeTeam(77) from the stock menu gets the same retail line as /team."""
    locked_mode = SimpleNamespace(
        allows_team_change=lambda player, team: False,
        configure_state_data=lambda packet: setattr(packet, "team2_locked", True),
    )
    server = _server(mode=locked_mode)
    mover = _player(server, 1, TEAM1)
    asyncio.run(team_handlers.handle_change_team(
        server, mover, SimpleNamespace(team=TEAM2)
    ))
    assert mover.team == TEAM1
    assert _localised_ids(mover) == ["TEAM_LOCKED"]
    assert _system_lines(mover) == []


def test_kill_command_keeps_domination_relations():
    from server import kill_feed

    server = _server()
    victim = _player(server, 1, TEAM1)
    bully = _player(server, 2, TEAM2)
    kill_feed._dominating(bully).add(int(victim.id))
    asyncio.run(player_cmds.cmd_kill(_ctx(server, victim)))
    assert int(victim.id) in kill_feed._dominating(bully)
