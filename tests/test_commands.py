"""Command-handler regression tests.

These exercise the command coroutines directly with lightweight fakes so the
three formerly-stubbed commands (/god, /ban, /ping) and a couple of core admin
commands stay working without needing a live client.
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

import commands.admin as admin
import commands.player as player_cmds
import commands.server_commands as server_cmds
from commands.command_handler import CommandContext, handle_command
from server.bans import BanManager, parse_duration
from server.handlers.social import handle_chat


# --- fakes -----------------------------------------------------------------

class FakePeer:
    def __init__(self, address="10.0.0.5", rtt=42):
        self.address = address
        self.roundTripTime = rtt


class FakeConnection:
    def __init__(self, peer):
        self.peer = peer


class FakePlayer:
    def __init__(self, name, admin=False, peer=None):
        self.name = name
        self.admin = admin
        self.god_mode = False
        self.muted = False
        self.alive = True
        self.x = self.y = self.z = 0.0
        self.connection = FakeConnection(peer) if peer else None
        self.disconnected = None

    def disconnect(self, reason=0):
        self.disconnected = reason

    def set_position(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class FakeConfig:
    admin_password = "long-enough-secret"
    log_commands = False


class FakeServer:
    def __init__(self, ban_manager):
        self.players = {}
        self.ban_manager = ban_manager
        self.config = FakeConfig()
        self.broadcasts = []

    def get_player_by_name(self, name):
        return self.players.get(name)

    def broadcast(self, data):
        self.broadcasts.append(data)


@pytest.fixture
def captured(monkeypatch):
    """Capture (player_name, message) tuples routed through send_message."""
    msgs = []

    async def fake_send(server, player, message):
        msgs.append((player.name, message))

    monkeypatch.setattr(admin, "send_message", fake_send)
    monkeypatch.setattr(player_cmds, "send_message", fake_send)
    monkeypatch.setattr(server_cmds, "send_message", fake_send)
    return msgs


def ctx(server, player, *args):
    raw = " ".join(args)
    return CommandContext(server=server, player=player, args=list(args), raw_args=raw)


def run(coro):
    return asyncio.run(coro)


# --- parse_duration --------------------------------------------------------

def test_parse_duration():
    assert parse_duration("30m") == 1800
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration("90") == 90
    assert parse_duration("perma") == 0
    assert parse_duration("") == 0
    assert parse_duration("cheating") == -1  # not a duration -> caller treats as reason


# --- /god ------------------------------------------------------------------

def test_kick_uses_retail_kicked_disconnect_reason(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    adminp = FakePlayer("Admin", admin=True)
    target = FakePlayer("Bob")
    server.players["Bob"] = target

    run(admin.cmd_kick(ctx(server, adminp, "Bob", "command-test")))

    assert target.disconnected == 2
    assert len(server.broadcasts) == 1


def test_god_toggles(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    p = FakePlayer("Admin", admin=True)
    server.players["Admin"] = p

    run(admin.cmd_god(ctx(server, p)))
    assert p.god_mode is True

    run(admin.cmd_god(ctx(server, p)))
    assert p.god_mode is False
    assert any("God mode" in m for _, m in captured)


# --- /ban ------------------------------------------------------------------

def test_ban_with_duration_persists(captured, tmp_path):
    bm = BanManager(str(tmp_path / "bans.json"))
    server = FakeServer(bm)
    adminp = FakePlayer("Admin", admin=True)
    target = FakePlayer("Bob", peer=FakePeer(address="10.0.0.5"))
    server.players["Bob"] = target

    run(admin.cmd_ban(ctx(server, adminp, "Bob", "30m", "spamming")))

    entry = bm.is_banned("10.0.0.5")
    assert entry is not None
    assert entry["reason"] == "spamming"
    assert entry["until"] > 0  # temporary ban has an expiry
    assert target.disconnected == 19  # ERROR_TEMP_BANNED (timed ban)
    assert len(server.broadcasts) == 1

    # A fresh manager over the same file still sees the ban (persistence).
    assert BanManager(str(tmp_path / "bans.json")).is_banned("10.0.0.5") is not None


def test_ban_permanent_when_no_duration(captured, tmp_path):
    bm = BanManager(str(tmp_path / "bans.json"))
    server = FakeServer(bm)
    adminp = FakePlayer("Admin", admin=True)
    target = FakePlayer("Bob", peer=FakePeer(address="10.0.0.5"))
    server.players["Bob"] = target

    # "cheating" is not a duration -> whole thing is the reason, permanent ban.
    run(admin.cmd_ban(ctx(server, adminp, "Bob", "cheating")))

    entry = bm.is_banned("10.0.0.5")
    assert entry is not None
    assert entry["reason"] == "cheating"
    assert entry["until"] == 0  # permanent


def test_ban_unknown_player(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    adminp = FakePlayer("Admin", admin=True)
    run(admin.cmd_ban(ctx(server, adminp, "Nobody")))
    assert any("not found" in m.lower() for _, m in captured)


# --- /ping -----------------------------------------------------------------

def test_ping_reports_rtt(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    p = FakePlayer("Bob", peer=FakePeer(rtt=57))
    run(player_cmds.cmd_ping(ctx(server, p)))
    assert any("57 ms" in m for _, m in captured)


def test_ping_no_connection(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    p = FakePlayer("BotZero", peer=None)  # bots have no peer
    run(player_cmds.cmd_ping(ctx(server, p)))
    assert any("unavailable" in m.lower() for _, m in captured)


# --- /mute -----------------------------------------------------------------

def test_mute_sets_flag(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    adminp = FakePlayer("Admin", admin=True)
    target = FakePlayer("Bob")
    server.players["Bob"] = target
    run(admin.cmd_mute(ctx(server, adminp, "Bob")))
    assert target.muted is True
    run(admin.cmd_unmute(ctx(server, adminp, "Bob")))
    assert target.muted is False


def test_muted_player_can_still_dispatch_unmute_command(monkeypatch):
    """Mute applies to conversation, never to the slash-command control path."""

    player = FakePlayer("Admin", admin=True)
    player.muted = True
    server = SimpleNamespace()
    dispatched = []

    async def fake_handle_command(observed_server, observed_player, message):
        dispatched.append((observed_server, observed_player, message))

    monkeypatch.setattr("commands.handle_command", fake_handle_command)
    packet = SimpleNamespace(value="/unmute Admin", chat_type=0)

    run(handle_chat(server, player, packet))

    assert dispatched == [(server, player, "unmute Admin")]


def test_local_ugc_title_bridge_is_private_host_only(monkeypatch):
    """The local settings bridge never leaks into commands or public chat."""

    player = FakePlayer("Builder")
    calls = []
    dispatched = []

    class Mode:
        def __init__(self, host):
            self.host = host

        def is_host(self, candidate):
            return candidate is self.host

        def set_title(self, candidate, title):
            calls.append((candidate, title))
            return True

    async def fake_handle_command(*args):
        dispatched.append(args)

    monkeypatch.setattr("commands.handle_command", fake_handle_command)
    server = SimpleNamespace(
        config=SimpleNamespace(ugc_runtime=True),
        mode=Mode(player),
    )
    packet = SimpleNamespace(
        value="/__local_ugc_title Edited City",
        chat_type=0,
    )

    run(handle_chat(server, player, packet))

    assert calls == [(player, "Edited City")]
    assert dispatched == []


def test_local_ugc_title_bridge_rejects_non_ugc_and_guest(monkeypatch):
    """Forged private controls are swallowed without mutating either server."""

    host = FakePlayer("Host")
    guest = FakePlayer("Guest")
    calls = []
    dispatched = []

    mode = SimpleNamespace(
        is_host=lambda player: player is host,
        set_title=lambda player, title: calls.append((player, title)),
    )

    async def fake_handle_command(*args):
        dispatched.append(args)

    monkeypatch.setattr("commands.handle_command", fake_handle_command)
    packet = SimpleNamespace(value="/__local_ugc_title Forged", chat_type=0)

    ordinary = SimpleNamespace(
        config=SimpleNamespace(ugc_runtime=False),
        mode=mode,
    )
    editor = SimpleNamespace(
        config=SimpleNamespace(ugc_runtime=True),
        mode=mode,
    )
    run(handle_chat(ordinary, host, packet))
    run(handle_chat(editor, guest, packet))

    assert calls == []
    assert dispatched == []


def test_bots_status_reports_worker_health(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    server.bots = SimpleNamespace(
        bots=[SimpleNamespace(), SimpleNamespace()],
        status=lambda: SimpleNamespace(
            running=True,
            process_id=1234,
            restarts=15,
            planned_recycles=15,
            crash_restarts=0,
            queued_frames=2,
            queued_intents=1,
            pending_terrain_cells=7,
        ),
    )
    adminp = FakePlayer("Admin", admin=True)

    run(server_cmds.cmd_bots(ctx(server, adminp, "status")))

    assert any("Bots=2 worker=up pid=1234" in message for _, message in captured)
    assert any("recycles=15 crashes=0" in message for _, message in captured)


def test_bots_difficulty_updates_new_profile_setting(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    server.config.bots = SimpleNamespace(difficulty="mixed")
    server.bots = SimpleNamespace(bots=[])
    adminp = FakePlayer("Admin", admin=True)

    run(server_cmds.cmd_bots(ctx(server, adminp, "difficulty", "hard")))

    assert server.config.bots.difficulty == "hard"
    assert any("difficulty set to hard" in message for _, message in captured)


# --- server administration -------------------------------------------------

class _TransitionService:
    def __init__(self):
        self.calls = []

    async def change_map(self, name):
        self.calls.append(("map", name))
        return type("Result", (), {
            "ok": True,
            "message": f"Map changed to {name}",
            "reconnect_required": True,
        })()

    async def change_mode(self, name):
        self.calls.append(("mode", name))
        return type("Result", (), {
            "ok": True,
            "message": f"Mode changed to {name}",
            "reconnect_required": True,
        })()

    async def restart_round(self):
        self.calls.append(("restart", None))
        return type("Result", (), {
            "ok": True,
            "message": "Match restarted",
            "reconnect_required": False,
        })()


def test_map_mode_and_restart_commands_use_transition_service(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    server.match_transition = _TransitionService()
    server.world_manager = type("World", (), {"map_name": "London"})()
    server.mode = type("Mode", (), {"name": "CTF"})()
    adminp = FakePlayer("Admin", admin=True)

    run(server_cmds.cmd_map(ctx(server, adminp, "HallwayPin")))
    run(server_cmds.cmd_mode(ctx(server, adminp, "tdm")))
    run(server_cmds.cmd_restart(ctx(server, adminp)))

    assert server.match_transition.calls == [
        ("map", "HallwayPin"),
        ("mode", "tdm"),
        ("restart", None),
    ]


def test_fog_rejects_out_of_range_without_wrapping(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    adminp = FakePlayer("Admin", admin=True)

    run(server_cmds.cmd_fog(ctx(server, adminp, "-1", "256", "20")))

    assert server.broadcasts == []
    assert any("0-255" in message for _, message in captured)


def test_fog_persists_for_reconnecting_clients(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    server.config.fog_color_rgb = (0, 0, 0)
    adminp = FakePlayer("Admin", admin=True)

    run(server_cmds.cmd_fog(ctx(server, adminp, "12", "34", "56")))

    assert server.fog_color_override == (12, 34, 56)
    assert server.config.fog_color_rgb == (0, 0, 0)
    assert len(server.broadcasts) == 1


def test_time_sets_new_remaining_window(captured, tmp_path, monkeypatch):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    server.mode = type("Mode", (), {
        "time_limit": 1200,
        "elapsed_time": 900.0,
        "start_time": 1.0,
    })()
    adminp = FakePlayer("Admin", admin=True)
    monkeypatch.setattr(server_cmds.time, "time", lambda: 5000.0)

    run(server_cmds.cmd_time(ctx(server, adminp, "300")))

    assert server.mode.time_limit == 300
    assert server.mode.elapsed_time == 0.0
    assert server.mode.start_time == 5000.0


def test_tp_rejects_non_finite_or_out_of_world_coordinates(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    adminp = FakePlayer("Admin", admin=True)

    run(admin.cmd_teleport(ctx(server, adminp, "nan", "20", "30")))
    run(admin.cmd_teleport(ctx(server, adminp, "9999", "20", "30")))

    assert (adminp.x, adminp.y, adminp.z) == (0.0, 0.0, 0.0)
    assert sum("coordinates" in message.lower() for _, message in captured) == 2


def test_say_rejects_oversized_announcements(captured, tmp_path):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    adminp = FakePlayer("Admin", admin=True)

    run(server_cmds.cmd_say(ctx(server, adminp, "x" * 300)))

    assert server.broadcasts == []
    assert any("too long" in message.lower() for _, message in captured)


def test_balance_uses_the_retail_safe_balancer(captured, tmp_path, monkeypatch):
    """Admin /balance: same rules as auto-balance (dead only, never a kill,
    TEAM_FULL to each moved player), reported to the admin."""
    from server.team_balance import TeamBalancer
    import server.handlers.team as team_handlers

    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    team1, team2 = server_cmds.TEAM1, server_cmds.TEAM2
    roster = []
    for index in range(5):
        player = FakePlayer(f"P{index}")
        player.id, player.team, player.is_bot = index, team1, False
        player.connection = SimpleNamespace(in_game=True)
        player.sent = []
        player.send = player.sent.append
        roster.append(player)
    other = FakePlayer("Other")
    other.id, other.team, other.is_bot = 9, team2, False
    roster.append(other)
    server.players = {player.id: player for player in roster}
    server.mode = SimpleNamespace(started=True, ended=False)
    server.bots = None
    # P3 and P4 are dead; everyone else is alive and must not be killed.
    for player in roster:
        player.alive = player.name not in ("P3", "P4")
    moves = []

    def fake_change_team(srv, player, new_team, *, force=False, explain=False):
        assert force and not player.alive
        moves.append(player.name)
        player.team = new_team
        return True

    monkeypatch.setattr(team_handlers, "change_team", fake_change_team)
    server.team_balance = TeamBalancer(server)

    run(server_cmds.cmd_balance(ctx(server, roster[0])))

    assert sorted(moves) == ["P3", "P4"]
    assert all(player.alive for player in roster if player.name not in ("P3", "P4"))
    assert any(b"TEAM_FULL" in data for name in ("P3", "P4")
               for data in server.players[int(name[1])].sent)
    assert any("2 player(s) moved" in message for _, message in captured)


def test_balance_never_kills_live_players(captured, tmp_path, monkeypatch):
    from server.team_balance import TeamBalancer

    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    team1, team2 = server_cmds.TEAM1, server_cmds.TEAM2
    roster = []
    for index in range(4):
        player = FakePlayer(f"P{index}")
        player.id, player.is_bot = index, False
        player.team = team1 if index else team2
        player.connection = SimpleNamespace(in_game=True)
        roster.append(player)
    server.players = {player.id: player for player in roster}
    server.mode = SimpleNamespace(started=True, ended=False)
    server.bots = None
    server.team_balance = TeamBalancer(server)
    run(server_cmds.cmd_balance(ctx(server, roster[0])))
    assert all(player.alive for player in roster)
    assert [player.team for player in roster] == [team2, team1, team1, team1]
    assert any("0 player(s) moved" in message and "never moved" in message
               for _, message in captured)


def test_admin_password_is_redacted_from_command_log(
    captured, tmp_path, caplog
):
    server = FakeServer(BanManager(str(tmp_path / "bans.json")))
    server.config.log_commands = True
    player = FakePlayer("Admin")

    with caplog.at_level(logging.INFO, logger="commands.command_handler"):
        run(handle_command(server, player, "admin long-enough-secret"))

    assert player.admin is True
    assert "long-enough-secret" not in caplog.text
    assert "<redacted>" in caplog.text
