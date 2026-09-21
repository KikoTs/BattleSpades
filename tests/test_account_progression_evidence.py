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
    players, _ = service._result_players(2)
    evidence = players[0]["participation"]
    assert evidence["result"] == "unfinished"
    assert evidence["active_seconds"] == 300 and evidence["bot_opponents"] == 4
