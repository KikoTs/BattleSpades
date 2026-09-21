from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from server.revival_master import (
    JoinTicketRejected,
    RevivalIdentity,
    RevivalMasterError,
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


def test_round_score_reset_rebases_match_counters_without_losing_profile_totals():
    server = make_server()
    player = SimpleNamespace(id=7, kills=0, deaths=0, captures=0, score=0)
    server.players[7] = player
    service = RevivalMasterService(server)
    service._player_baselines[id(player)] = (8, 4, 1, 850)
    service._profile_baselines[id(player)] = {5: [2, 100]}
    service.reset_scoreboard_baselines()
    player.kills, player.score = 1, 100
    assert service._player_delta(player) == (1, 0, 0, 100)
    assert service._profile_baselines[id(player)] == {5: [2, 100]}


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


@pytest.mark.parametrize("value, expected", [(2, 2), (3, 3), (0, None), (1, None),
                                             (True, None), ("3", None), (None, None)])
def test_lobby_assignment_is_optional_and_strict(value, expected):
    assert RevivalIdentity.from_payload(identity_payload(assigned_team=value)).assigned_team == expected


def test_heartbeat_identifier_matches_direct_connect_identifier(monkeypatch):
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    service = RevivalMasterService(make_server())
    payload = service.heartbeat_payload()
    assert service.server_id == "127.0.0.1:27015"
    assert payload["identifier"] == service.server_id
    assert payload["port"] == 27015
    assert "identity=ticket-v1" in payload["tags"]


def test_heartbeat_advertises_tunnel_mapped_public_ports(monkeypatch):
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    monkeypatch.setenv("AOS_PUBLIC_HOST", "147.185.221.26")
    monkeypatch.setenv("AOS_PUBLIC_PORT", "56675")
    monkeypatch.setenv("AOS_PUBLIC_QUERY_PORT", "56675")
    monkeypatch.setenv("AOS_SERVER_ID", "147.185.221.26:56675")

    service = RevivalMasterService(make_server())
    payload = service.heartbeat_payload()

    assert service.server_id == "147.185.221.26:56675"
    assert payload["port"] == 56675
    assert payload["queryPort"] == 56675
    assert "public_port_mapped" in payload["tags"]
    assert "listen_port=27015" in payload["tags"]


def test_invalid_public_port_fails_closed(monkeypatch):
    monkeypatch.setenv("AOS_PUBLIC_PORT", "not-a-port")
    with pytest.raises(RevivalMasterError, match="AOS_PUBLIC_PORT"):
        RevivalMasterService(make_server()).heartbeat_payload()


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


def result_service(tmp_path, monkeypatch):
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    server = make_server()
    server.config.revival.results_path = str(tmp_path / "rounds.sqlite3")
    player = SimpleNamespace(
        name="Builder", account_legacy_id="1000000000", account_nickname="Builder",
        kills=4, deaths=2, captures=0, score=17, team=2, is_bot=False,
    )
    server.players = {0: player}
    return RevivalMasterService(server), player


@pytest.mark.parametrize("mode", ["ugc", "tutorial", "tut"])
def test_noncompetitive_online_sessions_never_capture_profile_results(tmp_path, monkeypatch, mode):
    service, _ = result_service(tmp_path, monkeypatch)
    service.server.config.default_mode = mode
    service._capture_round_results(2)
    assert not service._pending_results


def test_lost_response_retries_identical_durable_event_after_restart(tmp_path, monkeypatch):
    service, _player = result_service(tmp_path, monkeypatch)
    attempts = []

    async def lost_ack(path, payload):
        attempts.append(payload)
        raise RevivalMasterError("response lost after remote commit")

    service._post = lost_ack

    async def scenario():
        with pytest.raises(RevivalMasterError):
            await service.submit_round_results(2)
        await service.close()
        restarted, _ = result_service(tmp_path, monkeypatch)
        restarted.server.players.clear()

        async def accept(path, payload):
            if path == "/api/master/servers/heartbeat":
                return 200, {"accepted": True}
            assert path == "/api/master/stats"
            attempts.append(payload)
            return 200, {"accepted": True, "duplicate": True}

        restarted._post = accept
        await restarted.start()
        await restarted._result_task
        assert attempts[0] == attempts[1]
        assert restarted._result_outbox.pending(restarted.server_id) == []
        await restarted.close()

    asyncio.run(scenario())


def test_overlapping_submissions_reserve_counters_once(tmp_path, monkeypatch):
    service, player = result_service(tmp_path, monkeypatch)
    attempts = []

    async def accept(path, payload):
        attempts.append(payload)
        await asyncio.sleep(0)
        return 200, {"accepted": True}

    service._post = accept

    async def scenario():
        await asyncio.gather(service.submit_round_results(2), service.submit_round_results(2))
        assert len(attempts) == 1
        player.kills += 1
        player.score += 5
        await service.submit_round_results(3)
        assert len(attempts) == 2
        assert attempts[1]["players"][0]["stats"]["1"] == [1, 1]
        assert attempts[1]["players"][0]["total"] == [1, 5]
        await service.close()

    asyncio.run(scenario())


def test_schedule_freezes_round_before_async_work_and_close_persists(tmp_path, monkeypatch):
    service, player = result_service(tmp_path, monkeypatch)

    async def scenario():
        service.server.config.default_mode = "oc"
        service.schedule_round_results(2)
        player.kills = 0
        player.score = 0
        player.team = 3
        service.server.config.default_mode = "tdm"
        await service.close()
        events = service._result_outbox.pending(service.server_id)
        assert len(events) == 1
        stats = events[0]["players"][0]["stats"]
        assert stats["1"] == [4, 4]
        assert stats["195"] == [1, 17]
        assert stats["159"] == [1, 0]
        assert "192" not in stats

    asyncio.run(scenario())


def test_departure_while_first_round_is_in_flight_keeps_only_new_delta(tmp_path, monkeypatch):
    service, player = result_service(tmp_path, monkeypatch)
    attempts = []

    async def accept(path, payload):
        attempts.append(payload)
        if len(attempts) == 1:
            player.kills += 2
            player.score += 8
            service.accumulate_departing_player(player)
            service.server.players.clear()
        return 200, {"accepted": True}

    service._post = accept

    async def scenario():
        await service.submit_round_results(2)
        await service.submit_round_results(2)
        assert len(attempts) == 2
        assert attempts[1]["players"][0]["stats"]["1"] == [2, 2]
        assert attempts[1]["players"][0]["total"] == [1, 8]
        await service.close()

    asyncio.run(scenario())


def test_disk_failure_retains_snapshot_without_submitting_unpersisted_event(tmp_path, monkeypatch):
    service, _player = result_service(tmp_path, monkeypatch)
    attempts = []
    persist = service._result_outbox.put_many

    def fail_write(events):
        raise OSError("disk unavailable")

    async def accept(path, payload):
        attempts.append(payload)
        return 200, {"accepted": True}

    service._post = accept
    service._result_outbox.put_many = fail_write

    async def scenario():
        with pytest.raises(OSError):
            await service.submit_round_results(2)
        snapshot = next(iter(service._pending_results.values()))
        assert not attempts
        service._result_outbox.put_many = persist
        await service.submit_round_results(2)
        assert attempts == [snapshot]
        await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("configured, canonical, mode_stat", [
    ("zombie", "zom", "198"), ("classic_ctf", "cctf", "197"),
    ("classic-ctf", "cctf", "197"), ("tdm", "tdm", "192"),
])
def test_relay_results_match_advertised_mode_and_keep_authenticated_evidence(
    tmp_path, monkeypatch, configured, canonical, mode_stat
):
    service, player = result_service(tmp_path, monkeypatch)
    service.server.config.default_mode = configured
    service.server.world_manager = SimpleNamespace(map_file_crc=0xAABBCCDD)
    service._round_started_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    player.ranked_eligible = False  # Community XP does not require an official/ranked host.
    player.profile_stats = SimpleNamespace(
        values={1: [4, 400]}, connected_seconds=100.0, active_seconds=90.0,
        afk_seconds=10.0, human_opponents=1, bot_opponents=0,
    )
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "aos_lobby_" + "a" * 43)
    monkeypatch.setenv("AOS_RELAY_LOBBY_ID", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setenv("AOS_PUBLIC_HOST", "relay.example.test")
    monkeypatch.setenv("AOS_PUBLIC_PORT", "40000")
    monkeypatch.setenv("AOS_SERVER_ID", "relay.example.test:40000")
    attempts = []

    async def accept(path, payload):
        assert path == "/api/master/stats"
        attempts.append(payload)
        return 200, {"accepted": True, "updated": 1}

    service._post = accept

    async def scenario():
        await service.submit_round_results(2)
        assert len(attempts) == 1
        result = attempts[0]
        assert result["server_id"] == service.heartbeat_payload()["identifier"]
        assert result["match"]["mode_id"] == service.heartbeat_payload()["mode_tla"] == canonical
        assert result["relay_lobby_id"] == "11111111-1111-4111-8111-111111111111"
        assert result["match"]["map_crc"] == "aabbccdd"
        assert result["players"][0]["steamid"] == player.account_legacy_id
        assert result["players"][0]["stats"][mode_stat] == [1, 17]
        assert result["players"][0]["participation"] == {
            "connected_seconds": 100, "active_seconds": 90, "afk_seconds": 10,
            "human_opponents": 1, "bot_opponents": 0, "result": "win",
        }
        assert not service._result_outbox.pending(service.server_id)
        await service.submit_round_results(2)
        assert len(attempts) == 1, "retrying unchanged counters must not create another round award"
        await service.close()

    asyncio.run(scenario())


def test_private_local_match_never_publishes_or_captures_account_results(tmp_path, monkeypatch):
    service, _ = result_service(tmp_path, monkeypatch)
    service.server.config.revival.enabled = False
    service._capture_round_results(2)
    assert not service._pending_results

    async def unexpected_post(*_args):
        raise AssertionError("private local session attempted a public master write")

    service._post = unexpected_post

    async def scenario():
        await service.start()
        await service.submit_round_results(2)
        await service.close()

    asyncio.run(scenario())
