"""Steam relay hosting for dedicated servers (server/steam_host.py)."""

from __future__ import annotations

import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from server.config import ServerConfig, load_config
from server.steam_host import (
    ACE_OF_SPADES_APP_ID,
    SPACEWAR_APP_ID,
    SteamHostService,
    _Helper,
    player_steam_id,
)
from server.steam_master import build_game_tags
from server.steam_p2p import SteamP2PService

PLAYER = "76561198158362762"
HOST = "85568392936826697"


class FakeHelper:
    def __init__(self, app_id: int) -> None:
        self.app_id = app_id
        self.steam_id = None
        self.sent: list[tuple[str, ...]] = []

    def send(self, *fields: str) -> None:
        self.sent.append(fields)

    def alive(self) -> bool:
        return True


class FakeBans:
    def __init__(self, banned=()) -> None:
        self.banned = set(banned)

    def is_banned(self, key):
        return object() if key in self.banned else None


def make_service(banned=(), kicked=()):
    disconnected = []
    server = SimpleNamespace(
        config=ServerConfig(),
        ban_manager=FakeBans(banned),
        vote_manager=SimpleNamespace(match_kick_reason=lambda key: "kicked" if key in kicked else None),
        connections={},
        disconnected=disconnected,
    )
    service = SteamHostService(server)
    server.steam_host = service
    helper = FakeHelper(ACE_OF_SPADES_APP_ID)
    service.helpers[ACE_OF_SPADES_APP_ID] = helper
    return server, service, helper


class FakePeer:
    """Hashable stand-in for an ENet peer."""

    def __init__(self, host: str, port: int) -> None:
        self.address = SimpleNamespace(host=host, port=port)


def peer_at(port: int):
    return FakePeer("127.0.0.1", port)


def test_player_steam_id_accepts_players_only():
    assert player_steam_id(PLAYER) == PLAYER
    for bad in ("", "abc", "0", HOST, "9" * 21, "76561197960265728"):
        with pytest.raises(ValueError):
            player_steam_id(bad)


def test_ready_publishes_the_host_id_and_relay_tags():
    server, service, helper = make_service()
    assert service.host_ids() == {}
    service._event(helper, ["READY", HOST, "0", "224540"])
    assert service.host_ids() == {ACE_OF_SPADES_APP_ID: HOST}
    assert server.config.steam_relay_tags == (f"sdr={HOST}",)

    spacewar = FakeHelper(SPACEWAR_APP_ID)
    service.helpers[SPACEWAR_APP_ID] = spacewar
    service._event(spacewar, ["READY", "90294212260083732", "1", "480"])
    assert server.config.steam_relay_tags == (f"sdr={HOST}", "sdr480=90294212260083732")


def test_a_player_is_allowed_and_mapped_to_a_steam_identity():
    server, service, helper = make_service()
    service._event(helper, ["PEER", "4242", "52065", PLAYER])
    assert helper.sent == [("ALLOW", "4242")]

    peer = peer_at(52065)
    assert service.identity_for(peer) == "steam:" + PLAYER
    # The shared lookup bans and vote kicks use sees the same identity.
    assert SteamP2PService(server).identity_for(peer) == "steam:" + PLAYER
    # A direct player on another loopback port is not a Steam player.
    assert service.identity_for(peer_at(40000)) is None
    assert service.identity_for(FakePeer("5.6.7.8", 52065)) is None


def test_banned_kicked_and_duplicate_routes_are_denied():
    key = "steam:" + PLAYER
    _, service, helper = make_service(banned=(key,))
    service._event(helper, ["PEER", "1", "50001", PLAYER])
    assert helper.sent == [("DENY", "1")] and not service.routes

    _, service, helper = make_service(kicked=(key,))
    service._event(helper, ["PEER", "2", "50002", PLAYER])
    assert helper.sent == [("DENY", "2")]

    _, service, helper = make_service()
    service._event(helper, ["PEER", "3", "50003", PLAYER])
    service._event(helper, ["PEER", "4", "50003", PLAYER])
    assert helper.sent == [("ALLOW", "3"), ("DENY", "4")]

    # Not a player account: the message is rejected, nothing is allowed.
    _, service, helper = make_service()
    with pytest.raises(ValueError):
        service._event(helper, ["PEER", "5", "50005", HOST])
    assert not helper.sent


def test_drop_forgets_the_route_and_disconnects_the_player():
    server, service, helper = make_service()
    service._event(helper, ["PEER", "7", "50007", PLAYER])
    peer = peer_at(50007)
    assert service.identity_for(peer) == "steam:" + PLAYER
    reasons = []
    server.connections[peer] = SimpleNamespace(disconnect=lambda reason: reasons.append(reason))

    service._event(helper, ["DROP", "7"])
    assert 50007 not in service.routes
    assert reasons == [8]
    # The identity stays pinned to the peer until the server forgets it.
    assert service.identity_for(peer) == "steam:" + PLAYER
    service.forget_peer(peer)
    assert service.identity_for(peer) is None


def test_a_stopped_helper_takes_its_players_and_tag_with_it():
    server, service, helper = make_service()
    service._event(helper, ["READY", HOST, "0", "224540"])
    service._event(helper, ["PEER", "9", "50009", PLAYER])
    service._forget_helper(ACE_OF_SPADES_APP_ID)
    assert not service.routes and service.host_ids() == {}
    assert server.config.steam_relay_tags == ()


def test_relay_tags_join_the_steam_game_tags_and_never_overflow():
    config = ServerConfig()
    plain = build_game_tags(config)
    config.steam_relay_tags = (f"sdr={HOST}", "sdr480=90294212260083732")
    tagged = build_game_tags(config)
    assert tagged == plain + f";sdr={HOST};sdr480=90294212260083732"
    assert len(tagged.encode()) < 128

    config.steam_relay_tags = ("sdr=" + "9" * 200,)
    assert build_game_tags(config) == plain  # too long: dropped, not fatal


def test_heartbeat_registers_the_relay_host_ids(monkeypatch):
    from server.revival_master import RevivalMasterService
    from tests.test_revival_master import make_server

    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    monkeypatch.delenv("AOS_STEAM_SIDECAR_STATUS", raising=False)
    server = make_server()
    service = RevivalMasterService(server)
    assert "steam_host_id" not in service.heartbeat_payload()

    server.steam_host = SimpleNamespace(host_ids=lambda: {224540: HOST, 480: "90294212260083732"})
    payload = service.heartbeat_payload()
    assert payload["steam_host_id"] == HOST
    assert payload["steam_host_id_480"] == "90294212260083732"


def test_config_section_is_validated(tmp_path):
    path = tmp_path / "server.toml"
    path.write_text(
        '[steam_host]\nenabled = true\napp_ids = [224540]\ntoken_file = "/run/gslt"\nmax_clients = 9999\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.steam_host.enabled and config.steam_host.app_ids == [224540]
    assert config.steam_host.token_file == "/run/gslt" and config.steam_host.max_clients == 256

    path.write_text("[steam_host]\napp_ids = []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_sidecar_passes_the_relay_tags_into_the_steam_listing():
    from scripts.steam_linux_sidecar import advertisement

    info = {
        "folder": "aceofspades", "name": "Test", "map": "TDM_Alcatraz", "players": 2, "bots": 0,
        "max_players": 24, "password": False, "version": "1.0.0.0", "port": 27015,
        "tags": f"v168;playlist=8;mode=0001;sdr={HOST};sdr480=90294212260083732;unknown=1",
    }
    tags = advertisement(info, "tdm", "europe")["tags"].split(";")
    assert f"sdr={HOST}" in tags and "sdr480=90294212260083732" in tags
    assert "unknown=1" not in tags


def test_helper_wrapper_reads_lines_and_writes_commands():
    script = (
        "import sys\n"
        "print('READY 85568392936826697 0 224540', flush=True)\n"
        "for line in sys.stdin:\n"
        "    print('LOG got ' + line.strip(), flush=True)\n"
        "    if line.strip() == 'QUIT':\n"
        "        break\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )
    helper = _Helper(ACE_OF_SPADES_APP_ID, process)
    helper.send("ALLOW", "12")

    lines = []
    deadline = time.monotonic() + 10
    while len(lines) < 2 and time.monotonic() < deadline:
        try:
            lines.append(helper.lines.get(timeout=0.2))
        except Exception:
            pass
    assert lines == ["READY 85568392936826697 0 224540", "LOG got ALLOW 12"]
    helper.stop()
    assert not helper.alive()
