"""HTTP-only reward evidence must remain honest across idling and round resets."""
from datetime import datetime, timedelta, timezone
import asyncio
import json
from types import SimpleNamespace

from server import profile_stats
from tests.test_profile_stats import player
from tests.test_revival_master import result_service


def test_closing_host_captures_unfinished_round_in_persistent_account_queue(tmp_path, monkeypatch):
    directory = tmp_path / "account-results"
    monkeypatch.setenv("AOS_RELAY_LOBBY_ID", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setenv("AOS_MATCH_RESULTS_DIRECTORY", str(directory))
    service, owner = result_service(tmp_path, monkeypatch)
    service._round_started_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    service.server.world_manager = SimpleNamespace(map_file_crc=0x1234ABCD)
    owner.profile_stats = profile_stats.ProfileStats(connected_seconds=110, active_seconds=100,
                                                   afk_seconds=10, bot_opponents=2)
    asyncio.run(service.close())
    reports = list(directory.glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["relay_lobby_id"] == "11111111-1111-4111-8111-111111111111"
    assert report["players"][0]["participation"]["result"] == "unfinished"
    asyncio.run(service.close())
    assert len(list(directory.glob("*.json"))) == 1


def test_idle_join_spectators_and_bots_do_not_create_active_time() -> None:
    owner = player()
    server = SimpleNamespace(players={1: owner}, config=SimpleNamespace(default_map="Unknown"))
    for _ in range(120):
        profile_stats.tick(owner, server, 1.0)
    assert owner.profile_stats.active_seconds == 0
    assert owner.profile_stats.afk_seconds == 120
    owner.position = (100.1, 100.0, 50.0)
    profile_stats.tick(owner, server, 1.0)
    assert owner.profile_stats.active_seconds == 1
    owner.team = 0
    owner.position = (100.2, 100.0, 50.0)
    profile_stats.tick(owner, server, 1.0)
    assert owner.profile_stats.active_seconds == 1
    bot = player(is_bot=True)
    profile_stats.tick(bot, server, 1.0)
    assert not hasattr(bot, "profile_stats")


def test_round_http_evidence_is_reserved_immutable_and_round_scoped(tmp_path, monkeypatch) -> None:
    service, owner = result_service(tmp_path, monkeypatch)
    service._round_started_at = datetime.now(timezone.utc) - timedelta(seconds=600)
    service.server.world_manager = SimpleNamespace(map_file_crc=0x1234ABCD)
    owner.profile_stats = profile_stats.ProfileStats(connected_seconds=600, active_seconds=500,
                                                   afk_seconds=100, human_opponents=2)
    service._capture_round_results(2)
    event = next(iter(service._pending_results.values()))
    match_id = event["match"]["match_id"]
    assert event["match"]["map_crc"] == "1234abcd"
    assert event["players"][0]["participation"] == {
        "connected_seconds": 600, "active_seconds": 500, "afk_seconds": 100,
        "human_opponents": 2, "bot_opponents": 0, "result": "win",
    }
    owner.profile_stats.active_seconds += 10
    assert event["players"][0]["participation"]["active_seconds"] == 500
    service.begin_round()
    assert service._round_match_id != match_id
    assert service._participation_delta(owner) == (0.0, 0.0, 0.0)
    assert owner.profile_stats.human_opponents == 0


def test_departure_preserves_time_but_does_not_award_match_completion(tmp_path, monkeypatch) -> None:
    service, owner = result_service(tmp_path, monkeypatch)
    owner.profile_stats = profile_stats.ProfileStats(connected_seconds=400, active_seconds=300,
                                                   afk_seconds=100, bot_opponents=4)
    service.accumulate_departing_player(owner)
    service.server.players.clear()
    players, _, _ = service._result_players(2)
    evidence = players[0]["participation"]
    assert evidence["result"] == "unfinished"
    assert evidence["active_seconds"] == 300 and evidence["bot_opponents"] == 4


def test_departure_checkpoint_rejoin_and_finish_keep_cumulative_evidence(tmp_path, monkeypatch):
    service, owner = result_service(tmp_path, monkeypatch)
    service._round_started_at = datetime.now(timezone.utc) - timedelta(seconds=600)
    service.server.world_manager = SimpleNamespace(map_file_crc=0x1234ABCD)
    owner.profile_stats = profile_stats.ProfileStats(connected_seconds=120, active_seconds=100,
                                                   afk_seconds=20)
    owner.profile_stats.values = {1: [4, 400]}
    reports = []

    async def accept(_path, payload):
        reports.append(payload)
        return 200, {"accepted": True}

    service._post = accept

    async def scenario():
        service.accumulate_departing_player(owner)
        service.accumulate_departing_player(owner)  # Duplicate disconnect callback.
        # A synchronous leave hook may inspect results before roster removal.
        players, _, _ = service._result_players(2)
        assert players[0]["stats"]["1"] == [4, 400]
        assert players[0]["participation"]["result"] == "unfinished"
        service.server.players.clear()
        service.schedule_departure_results()
        await service._result_task
        assert len(reports) == 1
        first = reports[0]
        assert first["match"]["cumulative"]
        assert first["players"][0]["participation"]["active_seconds"] == 100
        assert not service._round_completed
        # A new connection for the same durable account starts fresh counters.
        owner.kills, owner.score = 2, 8
        owner.profile_stats = profile_stats.ProfileStats(connected_seconds=90, active_seconds=90)
        owner.profile_stats.values = {1: [2, 200]}
        service.server.players[0] = owner
        await service.submit_round_results(2)
        assert len(reports) == 2
        final = reports[1]
        assert final["match"]["match_id"] == first["match"]["match_id"]
        assert final["players"][0]["stats"]["1"] == [6, 600]
        assert final["players"][0]["participation"]["active_seconds"] == 190
        assert final["players"][0]["participation"]["result"] == "win"
        assert first["players"][0]["stats"]["1"] == [4, 400]
        await service.close()

    asyncio.run(scenario())


def test_short_departure_keeps_counters_until_eligible_checkpoint(tmp_path, monkeypatch):
    service, owner = result_service(tmp_path, monkeypatch)
    service.server.world_manager = SimpleNamespace(map_file_crc=0x1234ABCD)
    owner.profile_stats = profile_stats.ProfileStats(connected_seconds=20, active_seconds=20)
    service.accumulate_departing_player(owner)
    service.server.players.clear()
    service._capture_round_results(None, finished=False)
    assert not service._pending_results
    assert service._departed
    service._round_started_at -= timedelta(seconds=60)
    service._capture_round_results(None, finished=False)
    report = next(iter(service._pending_results.values()))
    assert report["players"][0]["participation"]["active_seconds"] == 20


def test_many_departed_accounts_fit_api_limits(tmp_path, monkeypatch):
    service, owner = result_service(tmp_path, monkeypatch)
    service._round_started_at = datetime.now(timezone.utc) - timedelta(seconds=600)
    service.server.world_manager = SimpleNamespace(map_file_crc=0x1234ABCD)
    for index in range(130):
        visitor = SimpleNamespace(**vars(owner))
        visitor.account_legacy_id = str(1_000_000_000 + index)
        visitor.profile_stats = profile_stats.ProfileStats(connected_seconds=120, active_seconds=120)
        service.accumulate_departing_player(visitor)
    service.server.players.clear()
    service._capture_round_results(2)
    events = list(service._pending_results.values())
    assert len(events) == 2
    assert sum(len(event["players"]) for event in events) == 130
    assert all(len(event["players"]) <= 100 and len(json.dumps(event).encode()) < 256 * 1024
               for event in events)
    assert len({event["match"]["match_id"] for event in events}) == 1


def test_unchanged_departed_players_are_not_uploaded_at_every_checkpoint(tmp_path, monkeypatch):
    service, owner = result_service(tmp_path, monkeypatch)
    service._round_started_at = datetime.now(timezone.utc) - timedelta(seconds=600)
    service.server.world_manager = SimpleNamespace(map_file_crc=0x1234ABCD)
    owner.profile_stats = profile_stats.ProfileStats(connected_seconds=120, active_seconds=120)
    service.accumulate_departing_player(owner)
    service.server.players.clear()
    service._capture_round_results(None, finished=False)
    service._capture_round_results(None, finished=False)
    service._capture_round_results(2)
    assert len(service._pending_results) == 1


def test_custom_long_round_keeps_departure_xp_evidence(tmp_path, monkeypatch):
    service, owner = result_service(tmp_path, monkeypatch)
    service._round_started_at = datetime.now(timezone.utc) - timedelta(hours=8)
    service.server.world_manager = SimpleNamespace(map_file_crc=0x1234ABCD)
    owner.profile_stats = profile_stats.ProfileStats(connected_seconds=120, active_seconds=120)
    service.accumulate_departing_player(owner)
    service.server.players.clear()
    service._capture_round_results(None, finished=False)
    report = next(iter(service._pending_results.values()))
    assert report["match"]["duration_seconds"] >= 8 * 3600
    assert report["match"]["cumulative"]
    assert report["players"][0]["participation"]["active_seconds"] == 120
