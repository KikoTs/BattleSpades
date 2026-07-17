from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.revival_master import (
    JoinTicketRejected,
    RevivalIdentity,
    RevivalMasterService,
    is_join_code,
)


def make_server():
    config = SimpleNamespace(
        port=27015,
        max_players=24,
        server_name="Test Revival Server",
        default_map="MayanJungle",
        default_mode="tdm",
        revival=SimpleNamespace(
            enabled=True,
            base_url="https://www.aosplay.net",
            public_host="127.0.0.1",
            server_id="",
            region="europe",
            official=False,
            require_identity=False,
            heartbeat_interval_seconds=30.0,
            request_timeout_seconds=5.0,
        ),
        steam=SimpleNamespace(
            enabled=False,
            game_version="1.0.0.0",
            playlist_id=8,
            effective_query_port=lambda game_port: game_port + 1,
        ),
    )
    return SimpleNamespace(config=config, players={})


def identity_payload(**overrides):
    payload = {
        "public_id": "ply_abcdefghijklmnopqrstuv",
        "legacy_id": "1000000000",
        "nickname": "Builder",
        "account_type": "registered",
        "identity_type": "password",
        "ranked_eligible": True,
        "steam_id": None,
    }
    payload.update(overrides)
    return payload


def test_join_code_exactly_matches_retail_name_budget():
    assert is_join_code("~abcdefghijklmn")
    assert len("~abcdefghijklmn".encode("ascii")) == 15
    assert not is_join_code("aos_join_abcdefghijklmnopqrstuvwxyz")
    assert not is_join_code("~too-short")


def test_identity_payload_is_strictly_validated():
    identity = RevivalIdentity.from_payload(identity_payload())
    assert identity.legacy_id == "1000000000"
    assert identity.ranked_eligible is True
    with pytest.raises(JoinTicketRejected):
        RevivalIdentity.from_payload(identity_payload(legacy_id="spoofed"))


def test_heartbeat_identifier_matches_direct_connect_identifier(monkeypatch):
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    service = RevivalMasterService(make_server())
    payload = service.heartbeat_payload()
    assert service.server_id == "127.0.0.1:27015"
    assert payload["identifier"] == service.server_id
    assert payload["port"] == 27015
    assert "identity=ticket-v1" in payload["tags"]


def test_heartbeat_uses_live_map_mode_and_population(monkeypatch):
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    server = make_server()
    server.config.default_map = "ConfiguredMap"
    server.config.default_mode = "cctf"
    server.world_manager = SimpleNamespace(map_name="LiveMap")
    server.players = {
        0: SimpleNamespace(is_bot=False),
        1: SimpleNamespace(is_bot=False),
        2: SimpleNamespace(is_bot=True),
    }

    payload = RevivalMasterService(server).heartbeat_payload()

    assert payload["map"] == "LiveMap"
    assert payload["game_mode"] == "CCTF"
    assert payload["mode_tla"] == "cctf"
    assert payload["classic"] is True
    assert payload["players"] == 3
    assert payload["human_players"] == 2
    assert payload["bots"] == 1
    assert "mode=0008" in payload["tags"]


def test_consumed_join_code_returns_canonical_identity(monkeypatch):
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    service = RevivalMasterService(make_server())

    async def fake_post(path, payload):
        assert path == "/api/master/auth/consume-ticket"
        assert payload == {
            "ticket": "~abcdefghijklmn",
            "server_id": "127.0.0.1:27015",
        }
        return 200, {"authenticated": True, "player": identity_payload()}

    service._post = fake_post
    identity = asyncio.run(service.consume_join_ticket("~abcdefghijklmn"))
    assert identity.nickname == "Builder"
    assert identity.identity_type == "password"


def test_result_payload_uses_only_bound_non_bot_players(monkeypatch):
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    server = make_server()
    player = SimpleNamespace(
        name="WireName",
        account_nickname="CanonicalBuilder",
        account_legacy_id="1000000000",
        kills=4,
        deaths=2,
        captures=0,
        score=17,
        team=0,
        is_bot=False,
    )
    bot = SimpleNamespace(
        name="Bot",
        account_legacy_id=None,
        kills=99,
        deaths=0,
        captures=0,
        score=99,
        team=0,
        is_bot=True,
    )
    server.players = {0: player, 1: bot}
    service = RevivalMasterService(server)
    rows, _snapshots = service._result_players(winner=0)
    assert len(rows) == 1
    assert rows[0]["steamid"] == "1000000000"
    assert rows[0]["name"] == "CanonicalBuilder"
    assert rows[0]["stats"]["1"] == [4, 4]
    assert rows[0]["stats"]["192"] == [1, 17]
    assert rows[0]["stats"]["159"] == [1, 0]
