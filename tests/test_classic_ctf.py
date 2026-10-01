"""Retail compatibility tests for the dedicated Classic CTF ruleset."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
from modes import get_mode_class
from modes.classic_ctf import ClassicCTFMode
from server.builders.initial_info import build_initial_info
from server.builders.state_data import build_state_data
from server.class_selection import normalize_class_selection
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer

def _native_server() -> BattleSpadesServer:
    config = ServerConfig(default_mode="cctf")
    server = BattleSpadesServer(config)
    server.mode = ClassicCTFMode(server)
    return server


def test_classic_ctf_registry_uses_ctf_scene_with_classic_switches() -> None:
    server = _native_server()

    state = build_state_data(server, player_id=3)
    info = build_initial_info(server)

    assert get_mode_class("cctf") is ClassicCTFMode
    assert get_mode_class("classic_ctf") is ClassicCTFMode
    assert state.mode_type == 8  # native MODE_CTF; classic is a feature bit
    assert info.mode_key == 8
    assert info.classic == 1
    assert info.enable_minimap == 0
    assert info.allow_shooting_holding_intel == 1
    assert state.team1_classes == [int(C.CLASS_CLASSIC_SOLDIER)]
    assert state.team2_classes == [int(C.CLASS_CLASSIC_SOLDIER)]
    assert state.team1_locked_class is True
    assert state.team2_locked_class is True
    assert state.score_limit == 5
    assert server.mode.score_limit == 5
    assert int(C.CLASSIC_SMG_TOOL) in info.disabled_tools
    assert int(C.CLASSIC_SHOTGUN_TOOL) in info.disabled_tools


def test_classic_selection_forces_deuce_rifle_grenade_and_spade() -> None:
    mode = _native_server().mode
    untrusted = normalize_class_selection(
        int(C.CLASS_SOLDIER),
        (int(C.MINIGUN_TOOL), int(C.GRENADE_TOOL)),
    )

    selected = mode.prepare_join_selection(TEAM1, untrusted)

    assert selected.class_id == int(C.CLASS_CLASSIC_SOLDIER)
    assert int(C.RIFLE_TOOL) in selected.loadout
    assert int(C.CLASSIC_GRENADE_TOOL) in selected.loadout
    assert int(C.CLASSIC_SPADE_TOOL) in selected.loadout
    assert int(C.CLASSIC_SMG_TOOL) not in selected.loadout
    assert int(C.CLASSIC_SHOTGUN_TOOL) not in selected.loadout
    assert mode.allows_class_selection(
        SimpleNamespace(team=TEAM1), selected
    )


def test_classic_uses_recovered_intel_offset_and_stock_vote_catalog() -> None:
    server = _native_server()

    assert server.mode.intel_offset_from_base == 3.0
    # The catalogue is shuffled once at startup (retail random.shuffle).
    assert sorted(server.vote_manager._mode_available_maps()) == sorted(
        server.mode.stock_maps
    )


def test_classic_mode_specific_score_target_remains_operator_configurable() -> None:
    config = ServerConfig(
        default_mode="cctf",
        mode_settings={"cctf": {"score_limit": 7}},
    )
    server = BattleSpadesServer(config)
    server.mode = ClassicCTFMode(server)

    assert server.mode.score_limit == 7


def test_classic_dropped_intel_does_not_auto_return(monkeypatch) -> None:
    server = SimpleNamespace(
        config=SimpleNamespace(mode_settings={}),
        players={},
    )
    mode = ClassicCTFMode(server)
    server.mode = mode
    dropped_position = (210.0, 220.0, 50.0)
    mode.intel_positions[TEAM2] = dropped_position
    mode.intel_drop_time[TEAM2] = 100.0
    mode.start_time = 100.0
    monkeypatch.setattr("modes.ctf.time.time", lambda: 161.0)

    asyncio.run(mode.on_tick(1))

    assert mode.intel_auto_return is False
    assert mode.intel_positions[TEAM2] == dropped_position
    assert mode.intel_drop_time[TEAM2] == 100.0


# --- round clock -----------------------------------------------------------
# Kiril (2026-10-01): "If the timer reaches zero, it resets to 59 minutes."
# The retail HUD prints gmtime(countdown) as '%02d:%02d' (minutes, seconds):
# Classic's retail 90-minute clock showed 30:00, reached 00:00 an hour early
# and wrapped to 59:59 while the round went on.


def _retail_hud_text(timer: float) -> str:
    """hud.pyx draw_timer: '%02d:%02d' from time.gmtime (no hour field)."""

    import time as _time

    shown = _time.gmtime(timer)
    return "%02d:%02d" % (shown.tm_min, shown.tm_sec)


def _broadcast_timers(server) -> list:
    from shared.bytes import ByteReader
    from shared.packet import DisplayCountdown

    sent = []

    def broadcast(data, *args, **kwargs):
        if data and data[0] == DisplayCountdown.id:
            sent.append(float(DisplayCountdown(ByteReader(bytes(data)[1:])).timer))

    server.broadcast = broadcast
    return sent


def test_classic_clock_reaches_zero_only_when_the_round_ends() -> None:
    from server.scoreboard import send_round_timer

    server = _native_server()
    limit = float(server.mode.time_limit)
    assert limit == 5400.0  # retail CTF_CLASSIC_GAME_LENGTH stays the length
    sent = _broadcast_timers(server)

    # Every 1 Hz refresh of the whole round, then the HUD's local countdown
    # up to just before the next refresh.
    previous = None
    for elapsed in range(int(limit) + 1):
        sent.clear()
        send_round_timer(server, limit - elapsed, reliable=False)
        (timer,) = sent
        for local in (0.0, 0.5, 0.999):
            shown = _retail_hud_text(max(0.0, timer - local))
            minutes, seconds = (int(part) for part in shown.split(":"))
            total = minutes * 60 + seconds
            # The displayed clock never jumps back up (no 00:00 -> 59:59).
            assert previous is None or total <= previous, (elapsed, shown)
            previous = total
            if shown == "00:00":
                assert limit - elapsed - local < 1.0, (elapsed, local)
    assert _retail_hud_text(sent[0]) == "00:00"


def test_classic_clock_above_an_hour_holds_at_59_59() -> None:
    from server.scoreboard import send_round_timer

    server = _native_server()
    sent = _broadcast_timers(server)
    for remaining in (5400.0, 3600.0, 3599.5):
        send_round_timer(server, remaining)
    assert [_retail_hud_text(timer) for timer in sent] == ["59:59"] * 3
    sent.clear()
    send_round_timer(server, 3000.0)
    assert _retail_hud_text(sent[0]) == "50:00"


def test_classic_one_hz_refresh_is_hud_safe_and_round_ends_at_limit(
    monkeypatch,
) -> None:
    import time as _time

    from server.simulation_runtime import SimulationRuntime

    server = _native_server()
    mode = server.mode
    mode.started = True
    sent = _broadcast_timers(server)
    runtime = SimulationRuntime(server)

    now = 1_000_000.0
    monkeypatch.setattr(_time, "time", lambda: now)
    mode.start_time = now - 100.0  # 88:20 left
    mode.elapsed_time = 100.0
    server.loop_count = server.tick_rate * 10
    runtime._update_second_schedulers()
    assert sent and _retail_hud_text(sent[-1]) == "59:59"

    ended = []

    async def end_by_time():
        ended.append(mode.elapsed_time)
        mode.ended = True

    mode._end_by_time = end_by_time
    server._mode_events = []
    mode.start_time = now - (float(mode.time_limit) - 0.5)
    asyncio.run(mode.on_tick(1))
    assert not ended  # 00:00 is on screen, but half a second is left
    mode.start_time = now - float(mode.time_limit)
    asyncio.run(mode.on_tick(2))
    assert ended == [float(mode.time_limit)]
    assert mode.ended
