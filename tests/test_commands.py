"""Command-handler regression tests.

These exercise the command coroutines directly with lightweight fakes so the
three formerly-stubbed commands (/god, /ban, /ping) and a couple of core admin
commands stay working without needing a live client.
"""
import asyncio

import pytest

import commands.admin as admin
import commands.player as player_cmds
from commands.command_handler import CommandContext
from server.bans import BanManager, parse_duration


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
    admin_password = "secret"
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
    assert target.disconnected == 1  # DISCONNECT_BANNED
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
