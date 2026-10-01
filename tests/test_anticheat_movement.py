"""Anti-cheat coverage for the movement input pipeline (server/player.py).

Every behaviour change is gated by an ``[anticheat] enforce_*`` switch: with
the switch off the check must only report (``kind:observed``) and leave the
retail-parity pipeline untouched; with it on the exploit must be fixed.
"""

import asyncio
import math
import time
from types import SimpleNamespace

import pytest

import shared.constants as C
from server import anticheat
from server.config import ServerConfig
from server.handlers import movement
from server.player import EYE_HISTORY_LIMIT, Player
from tests.test_reversed_world_update import (
    DummyConnection,
    make_player,
    make_world_manager,
)


TICK_DT = 1.0 / 60.0
IDLE = (False,) * 8
FORWARD = (True, False, False, False, False, False, False, False)
ORIENTATION = (1.0, 0.0, 0.0)


class _Connection(DummyConnection):
    def __init__(self, server=None, player=None):
        super().__init__(server, player)
        self.disconnect_reasons: list[int] = []

    def disconnect(self, reason=0):
        self.disconnect_reasons.append(int(reason))


def _server(**anticheat_settings):
    config = ServerConfig()
    for name, value in anticheat_settings.items():
        setattr(config.anticheat, name, value)
    return SimpleNamespace(
        world_manager=make_world_manager(),
        players={},
        config=config,
        loop_count=1000,
        tick_rate=60,
    )


def _player(**anticheat_settings):
    server = _server(**anticheat_settings)
    player, connection = make_player(server)
    replacement = _Connection(server, player)
    player.connection = replacement
    return player, replacement, server


def _tick(player, dt=TICK_DT):
    asyncio.run(player.simulate_tick(dt))


def _counts(player):
    return dict(anticheat.counters(player))


# -- A: eye/aim history ------------------------------------------------------


def test_eye_history_records_each_applied_label():
    player, _, server = _player()
    orientations = {
        100: (1.0, 0.0, 0.0),
        101: (0.0, 1.0, 0.0),
        102: (0.0, 0.0, 1.0),
    }
    eyes = {}
    for label, aim in orientations.items():
        player.record_input_frame(
            label, FORWARD, aim, received_server_tick=server.loop_count
        )
        _tick(player)
        eyes[label] = player.eye
        assert player.applied_loop == label

    for label, aim in orientations.items():
        assert player.eye_at_loop(label) == pytest.approx(eyes[label])
        assert player.orientation_at_loop(label) == pytest.approx(aim)
    assert player.eye_at_loop(99) is None
    assert player.orientation_at_loop(103) is None
    assert player.eye_at_loop("junk") is None


def test_eye_history_covers_refilled_lost_frames_and_is_bounded():
    player, _, server = _player()
    player.record_input_frame(100, FORWARD, ORIENTATION)
    _tick(player)
    player.record_input_frame(102, FORWARD, ORIENTATION)
    _tick(player)  # refills 101

    assert player.last_applied_input_synthesized is True
    assert player.eye_at_loop(101) == pytest.approx(player.eye)

    for label in range(103, 103 + EYE_HISTORY_LIMIT + 10):
        player.record_input_frame(label, IDLE, ORIENTATION)
        _tick(player)
    assert player.eye_at_loop(100) is None
    # The refill left 102 queued, so consumption trails arrival by one.
    assert player.eye_at_loop(102 + EYE_HISTORY_LIMIT + 9) is not None
    assert len(player._eye_history) == EYE_HISTORY_LIMIT


def test_eye_history_is_cleared_by_spawn():
    player, _, _ = _player()
    player.record_input_frame(100, IDLE, ORIENTATION)
    _tick(player)
    assert player.eye_at_loop(100) is not None
    player.spawn(*player.position)
    assert player.eye_at_loop(100) is None
    assert player.applied_loop is None


# -- B: far-ahead labels, airborne starvation, starvation timeout ----------


def test_far_ahead_label_is_only_reported_in_log_only_mode():
    player, _, server = _player()
    player.record_input_frame(100, IDLE, ORIENTATION)
    _tick(player)

    forged = 2 ** 31 - 1
    player.record_input_frame(
        forged, IDLE, ORIENTATION, received_server_tick=server.loop_count
    )

    assert forged in player.input_history
    assert _counts(player).get("input_label_ahead:observed") == 1


def test_far_ahead_label_is_rejected_when_enforced_and_body_keeps_moving():
    player, _, server = _player(enforce_input_starvation=True)
    player.record_input_frame(100, IDLE, ORIENTATION)
    _tick(player)

    player.record_input_frame(
        2 ** 31 - 1, IDLE, ORIENTATION, received_server_tick=server.loop_count
    )
    assert player.input_history == {}
    assert _counts(player).get("input_label_ahead") == 1

    # The real stream still drives the body (before: frozen for the life).
    player.record_input_frame(
        101, FORWARD, ORIENTATION, received_server_tick=server.loop_count
    )
    _tick(player)
    assert player.last_applied_input_loop == 101


def test_clock_sync_relabel_near_the_server_loop_is_accepted_when_enforced():
    """A legit ClockSync jump lands near the server loop, not near 2^31."""
    player, _, server = _player(enforce_input_starvation=True)
    player.record_input_frame(100, IDLE, ORIENTATION)
    _tick(player)

    player.record_input_frame(
        server.loop_count + 20,
        IDLE,
        ORIENTATION,
        received_server_tick=server.loop_count,
    )
    _tick(player)

    assert player.last_applied_input_loop == server.loop_count + 20
    assert "input_label_ahead" not in _counts(player)


def _airborne_player(**settings):
    player, connection, server = _player(**settings)
    x, y, z = player.position
    player.record_input_frame(100, IDLE, ORIENTATION)
    _tick(player)
    player.set_position(x, y, z - 20.0)  # 20 blocks up (z grows downward)
    player.record_input_frame(101, IDLE, ORIENTATION)
    _tick(player)
    assert player.airborne
    return player, connection, server


def test_airborne_starvation_freezes_in_log_only_mode():
    player, _, _ = _airborne_player()
    frozen = player.position

    for _ in range(40):
        _tick(player)

    assert player.position == pytest.approx(frozen)
    assert player.last_applied_input_loop == 101
    assert _counts(player).get("input_starvation_airborne:observed") == 1


def test_airborne_starvation_applies_gravity_when_enforced():
    player, _, _ = _airborne_player(enforce_input_starvation=True)
    player.update_input(True, False, False, False, True, False, False, True)
    frozen = player.position

    for _ in range(23):
        _tick(player)
    assert player.position == pytest.approx(frozen)

    for _ in range(30):
        _tick(player)

    assert player.z > frozen[2] + 0.5  # fell
    assert player.input.up is False and player.input.jump is False
    # The acknowledged label never advances past what the client sent.
    assert player.last_applied_input_loop == 101
    assert player.input_frames_starvation_steps == 30
    assert _counts(player).get("input_starvation_airborne") == 1


def test_grounded_stall_keeps_the_seamless_resume_when_enforced():
    player, _, _ = _player(enforce_input_starvation=True)
    calls = []

    async def record_update(dt):
        calls.append(dt)

    player.update = record_update
    player.record_input_frame(100, FORWARD, ORIENTATION)
    _tick(player)
    assert calls == [pytest.approx(TICK_DT)]

    for _ in range(60):
        _tick(player)
    assert len(calls) == 1

    player.record_input_frame(161, FORWARD, ORIENTATION)
    _tick(player)
    assert player.last_applied_input_loop == 161
    assert len(calls) == 2


def _silence(player, seconds):
    old = time.monotonic() - seconds
    player._last_client_data_at = old
    player._starvation_baseline = old
    player.spawned_at = old
    player._last_simulated_at = time.monotonic()


def test_starvation_timeout_is_only_reported_in_log_only_mode():
    player, connection, _ = _player()
    _silence(player, 9.0)

    _tick(player)
    _tick(player)

    assert connection.disconnect_reasons == []
    assert _counts(player).get("input_starvation_timeout:observed") == 1


def test_starvation_timeout_disconnects_when_enforced():
    player, connection, _ = _player(enforce_input_starvation=True)
    _silence(player, 9.0)

    _tick(player)

    assert connection.disconnect_reasons == [int(C.DISCONNECT.ERROR_TIMEOUT)]


def test_starvation_timeout_rebases_after_a_simulation_pause():
    """A dead/rolling-over player is not simulated; do not count that gap."""
    player, connection, _ = _player(enforce_input_starvation=True)
    _silence(player, 9.0)
    player._last_simulated_at = time.monotonic() - 5.0

    _tick(player)

    assert connection.disconnect_reasons == []


def test_bots_never_starve_or_time_out():
    player, connection, _ = _player(enforce_input_starvation=True)
    player.is_bot = True
    _silence(player, 60.0)
    calls = []

    async def record_update(dt):
        calls.append(dt)

    player.update = record_update
    for _ in range(40):
        _tick(player)

    assert len(calls) == 40
    assert connection.disconnect_reasons == []
    assert anticheat.counters(player) == {}


# -- C: input backlog / fake lag ---------------------------------------------


def _fake_lag_run(player, server, ticks=240, withheld=90):
    """Withhold ``withheld`` frames, burst them, then stream at 60 Hz."""
    label = 100
    for _ in range(withheld):
        label += 1
    server.loop_count += withheld
    for queued in range(101, label + 1):
        player.record_input_frame(
            queued, IDLE, ORIENTATION, received_server_tick=server.loop_count
        )
    for _ in range(ticks):
        server.loop_count += 1
        label += 1
        player.record_input_frame(
            label, IDLE, ORIENTATION, received_server_tick=server.loop_count
        )
        _tick(player)
    return label


def _prime(player, server):
    player.record_input_frame(
        100, IDLE, ORIENTATION, received_server_tick=server.loop_count
    )
    _tick(player)


def test_backlog_is_only_reported_in_log_only_mode():
    player, _, server = _player()
    _prime(player, server)

    newest = _fake_lag_run(player, server)

    assert len(player.input_history) == 90
    assert player.last_applied_input_loop == newest - 90
    assert player.input_frames_catchup == 0
    assert player.input_frames_backlog_dropped == 0
    assert _counts(player).get("input_backlog:observed", 0) >= 1
    stats = player.input_queue_delay_stats()
    assert stats["max"] >= 90 and stats["p50"] >= 80


def test_backlog_catches_up_with_safe_double_steps_when_enforced():
    player, _, server = _player(enforce_input_backlog=True)
    _prime(player, server)

    newest = _fake_lag_run(player, server)

    assert len(player.input_history) <= 3
    assert player.last_applied_input_loop >= newest - 3
    assert player.input_frames_catchup > 0
    assert player.input_frames_backlog_dropped == 0
    assert _counts(player).get("input_backlog") == 1


def test_backlog_drops_oldest_when_a_transition_blocks_double_steps():
    player, _, server = _player(enforce_input_backlog=True)
    _prime(player, server)
    # A pending terrain mutation makes a two-frame batch unsafe.
    server.world_mutations = SimpleNamespace(pending_count=1)

    newest = _fake_lag_run(player, server)

    assert player.input_frames_catchup == 0
    assert player.input_frames_backlog_dropped > 0
    assert len(player.input_history) <= 6
    assert player.last_applied_input_loop >= newest - 6


def test_backlog_pair_is_unsafe_across_topology_change_or_jetpack():
    player, _, server = _player(enforce_input_backlog=True)
    _prime(player, server)
    for label in (101, 102):
        player.record_input_frame(
            label, IDLE, ORIENTATION, received_server_tick=server.loop_count
        )
    assert player._backlog_pair_safe(server) is True

    server.world_manager.topology_version += 1
    assert player._backlog_pair_safe(server) is False
    server.world_manager.topology_version -= 1

    player.jetpack_active = True
    assert player._backlog_pair_safe(server) is False
    player.jetpack_active = False

    player.jetpack_id = int(C.JETPACK2)
    player.input_history[102] = player.input_history[102].__class__(
        movement_flags=(False, False, False, False, True, False, False, False),
        orientation=ORIENTATION,
        received_server_tick=server.loop_count,
        topology_version=server.world_manager.topology_version,
    )
    assert player._backlog_pair_safe(server) is False


def test_steady_jittery_stream_never_triggers_backlog_when_enforced():
    player, _, server = _player(enforce_input_backlog=True)
    _prime(player, server)
    label = 100
    for tick in range(600):
        server.loop_count += 1
        # Jitter: every 10th tick delivers nothing, the next delivers two.
        if tick % 10 == 9:
            _tick(player)
            continue
        count = 2 if tick % 10 == 0 and tick else 1
        for _ in range(count):
            label += 1
            player.record_input_frame(
                label, IDLE, ORIENTATION, received_server_tick=server.loop_count
            )
        _tick(player)

    assert player.input_frames_catchup == 0
    assert player.input_frames_backlog_dropped == 0
    assert "input_backlog" not in _counts(player)
    assert player.input_queue_delay_stats()["max"] <= 2


# -- D: launcher reload skip -------------------------------------------------


def _launcher_player():
    player = Player.__new__(Player)
    player.id = 3
    player.name = "Launcher"
    player.blocks = 10
    player.is_bot = False
    Player._reset_equipment_state(player)
    return player


@pytest.mark.parametrize(
    "tool, reload_gap",
    [
        (C.RPG_TOOL, 0.7 + 1.5),
        (C.GRENADE_LAUNCHER_WEAPON_TOOL, 0.35 + 2.0),
        (C.MINE_LAUNCHER_TOOL, 0.35 + 2.0),
        (C.DRILLGUN_TOOL, 0.2 + 4.0),
    ],
)
def test_single_round_launchers_require_a_reload_between_shots(tool, reload_gap):
    player = _launcher_player()
    assert Player.consume_oriented_item(player, tool, now=10.0)

    # Cadence alone (the old gate) would allow this shot.
    early = 10.0 + 0.75
    assert not Player.can_use_oriented_item(player, tool, now=early)
    assert not Player.consume_oriented_item(player, tool, now=early)
    assert anticheat.counters(player)["launcher_reload_skip"] == 1

    assert Player.consume_oriented_item(player, tool, now=10.0 + reload_gap)


def test_rpg_burst_of_four_is_capped_to_legit_cadence():
    player = _launcher_player()
    fired = [
        t for t in (0.0, 0.7, 1.4, 2.1)
        if Player.consume_oriented_item(player, C.RPG_TOOL, now=100.0 + t)
    ]
    # Only the reload-length gap admits a second rocket.
    assert fired == [0.0, 2.1]
    # Legit retail: four rockets in ~6.6 s.
    player = _launcher_player()
    fired = [
        t for t in (0.0, 2.2, 4.4, 6.6)
        if Player.consume_oriented_item(player, C.RPG_TOOL, now=100.0 + t)
    ]
    assert fired == [0.0, 2.2, 4.4, 6.6]


def test_rpg2_clip_of_three_then_reload():
    player = _launcher_player()
    for t in (0.0, 0.8, 1.6):
        assert Player.consume_oriented_item(player, C.RPG2_TOOL, now=50.0 + t)
    assert not Player.can_use_oriented_item(player, C.RPG2_TOOL, now=52.4)
    assert Player.consume_oriented_item(player, C.RPG2_TOOL, now=51.6 + 1.75)


def test_rpg2_manual_partial_reload_is_not_flagged():
    player = _launcher_player()
    assert Player.consume_oriented_item(player, C.RPG2_TOOL, now=0.0)
    # Player pressed R; after a reload-length pause the clip is full again.
    for t in (2.0, 2.8, 3.6):
        assert Player.consume_oriented_item(player, C.RPG2_TOOL, now=t)
    assert "launcher_reload_skip" not in anticheat.counters(player)


def test_bots_are_exempt_from_the_launcher_reload_model():
    player = _launcher_player()
    player.is_bot = True
    assert Player.consume_oriented_item(player, C.RPG_TOOL, now=0.0)
    assert Player.consume_oriented_item(player, C.RPG_TOOL, now=0.8)


def test_grenades_keep_cadence_only():
    player = _launcher_player()
    assert Player.consume_oriented_item(player, C.GRENADE_TOOL, now=0.0)
    assert Player.consume_oriented_item(player, C.GRENADE_TOOL, now=1.5)


# -- E: orientation hardening and crouch hitbox ------------------------------


def test_non_finite_orientation_keeps_the_previous_aim():
    player, _, _ = _player()
    player.set_orientation_vector(0.0, 1.0, 0.0)
    player.set_orientation_vector(float("nan"), 0.0, 0.0)
    player.set_orientation_vector(float("inf"), 1.0, 0.0)
    assert player.orientation == pytest.approx((0.0, 1.0, 0.0))
    assert anticheat.counters(player)["orientation_nonfinite"] == 2


def test_non_finite_buffered_orientation_is_replaced_by_the_previous_aim():
    player, _, _ = _player()
    player.record_input_frame(100, IDLE, (0.0, 1.0, 0.0))
    _tick(player)
    player.record_input_frame(101, IDLE, (float("nan"), 0.0, 0.0))

    stored = player.input_history[101].orientation
    assert all(math.isfinite(value) for value in stored)
    _tick(player)
    assert all(math.isfinite(value) for value in player.orientation)


def test_hitbox_crouched_follows_the_simulated_body():
    player, _, _ = _player()
    assert player.hitbox_crouched is False
    player.record_input_frame(
        100, (False, False, False, False, False, True, False, False), ORIENTATION
    )
    _tick(player)
    assert player._world_object.crouch is True
    assert player.hitbox_crouched is True

    # Raw input alone never changes the hitbox before physics applies it.
    player.input.crouch = False
    assert player.hitbox_crouched is True


# -- F: MG unmount and rejected tool updates ---------------------------------


def test_machine_gun_unmount_restores_the_previous_tool():
    player, _, _ = _player()
    player.set_tool(int(C.RIFLE_TOOL), raw=True)
    player.mounted_entity_id = 7
    player.set_tool(int(C.MG_TOOL), raw=True)
    player.mounted_entity_id = None

    assert player.on_machine_gun_unmounted() == int(C.RIFLE_TOOL)
    assert player.tool == int(C.RIFLE_TOOL)


def test_machine_gun_unmount_keeps_a_non_mg_tool():
    player, _, _ = _player()
    player.set_tool(int(C.BLOCK_TOOL), raw=True)
    assert player.on_machine_gun_unmounted() is None
    assert player.tool == int(C.BLOCK_TOOL)


def _client_data(tool_id, loop=100):
    return SimpleNamespace(
        up=False, down=False, left=False, right=False, jump=False,
        crouch=False, sneak=False, sprint=False,
        loop_count=loop, o_x=1.0, o_y=0.0, o_z=0.0,
        primary=False, secondary=False, zoom=False, can_pickup=False,
        can_display_weapon=True, is_on_fire=False, is_weapon_deployed=False,
        hover=False, palette_enabled=False, ooo=0, tool_id=tool_id,
    )


def test_rejected_tool_update_falls_back_from_an_illegal_held_tool():
    player, _, server = _player()
    player.tool = int(C.MG_TOOL)  # stale: no mount authorizes it any more

    asyncio.run(movement.handle_client_data(
        server, player, _client_data(int(C.RPG_TOOL))
    ))

    assert player.tool == int(C.RIFLE_TOOL)
    assert player.rejected_tool_updates == 1
    assert anticheat.counters(player)["tool_rejected"] == 1


def test_rejected_tool_update_keeps_a_legal_held_tool():
    player, _, server = _player()
    player.set_tool(int(C.BLOCK_TOOL), raw=True)

    asyncio.run(movement.handle_client_data(
        server, player, _client_data(int(C.RPG_TOOL))
    ))

    assert player.tool == int(C.BLOCK_TOOL)


# -- G: per-peer packet rates ------------------------------------------------


def _peer():
    return SimpleNamespace(id=1, name="peer", is_bot=False)


def test_legit_60hz_client_data_with_jitter_bursts_is_never_dropped():
    server = _server()
    peer = _peer()
    now = 0.0
    for second in range(20):
        # 60 packets per second delivered in uneven clumps of 1..12.
        clumps = [1, 12, 3, 8, 1, 1, 10, 4, 6, 2, 12]
        sent = 0
        index = 0
        while sent < 60:
            count = min(clumps[index % len(clumps)], 60 - sent)
            for _ in range(count):
                assert movement._rate_allowed(server, peer, "client_data", now)
            sent += count
            index += 1
            now += 1.0 / 11.0 * (count / 6.0)
        now = float(second + 1)
    assert anticheat.counters(peer) == {}


def test_server_hitch_backlog_of_client_data_is_admitted():
    server = _server()
    peer = _peer()
    assert movement._rate_allowed(server, peer, "client_data", 0.0)
    # Two seconds of 60 Hz frames drained in one tick after a hitch.
    for _ in range(120):
        assert movement._rate_allowed(server, peer, "client_data", 2.0)


def test_client_data_flood_is_dropped_and_reported():
    server = _server()
    peer = _peer()
    allowed = sum(
        movement._rate_allowed(server, peer, "client_data", 5.0)
        for _ in range(1000)
    )
    assert allowed == 64
    assert anticheat.counters(peer)["rate:client_data"] == 936


def test_clock_sync_flood_is_dropped_but_periodic_sync_is_not():
    server = _server()
    peer = _peer()
    for second in range(30):
        assert movement._rate_allowed(server, peer, "clock_sync", float(second))
    flood = sum(
        movement._rate_allowed(server, peer, "clock_sync", 31.0)
        for _ in range(200)
    )
    assert flood <= 17


def test_dropped_client_data_does_not_reach_the_player():
    player, _, server = _player()
    for _ in range(64):
        asyncio.run(movement.handle_client_data(
            server, player, _client_data(int(C.RIFLE_TOOL), loop=100)
        ))
    before = dict(player.input_history)
    player._packet_rate_buckets["client_data"] = (
        0.0, time.monotonic() + 3600.0, server.loop_count / 60.0
    )
    asyncio.run(movement.handle_client_data(
        server, player, _client_data(int(C.RIFLE_TOOL), loop=500)
    ))
    assert 500 not in player.input_history
    assert dict(player.input_history) == before
    assert anticheat.counters(player)["rate:client_data"] == 1


def test_simulated_ticks_earn_client_data_budget_without_wall_time():
    """Offline replays step the loop faster than real time (one frame/tick)."""
    server = _server()
    peer = _peer()
    for _ in range(600):
        server.loop_count += 1
        assert movement._rate_allowed(server, peer, "client_data", 0.0)


def test_non_finite_position_report_is_ignored():
    player, _, server = _player()
    packet = SimpleNamespace(x=float("nan"), y=0.0, z=0.0)
    asyncio.run(movement.handle_position_data(server, player, packet))
    assert player.position_reports_received == 0
    assert anticheat.counters(player)["position_nonfinite"] == 1


# -- held-trigger cadence (retail fires continuously while LMB is held) -------


def test_held_rpg2_clip_lands_at_its_interval_with_arrival_jitter():
    """Character.update_weapon fires every update while shoot_primary is set,
    so a held RPG2 sends its three rockets one shoot_interval apart. Packets
    arriving a little early (jitter) still count, but the schedule never lets
    the sustained rate exceed one rocket per interval."""
    player = _launcher_player()
    interval = 0.75
    assert Player.consume_oriented_item(player, C.RPG2_TOOL, now=10.0)
    # Second packet 30 ms early (jitter), third on schedule.
    assert Player.consume_oriented_item(player, C.RPG2_TOOL, now=10.0 + interval - 0.03)
    assert Player.consume_oriented_item(player, C.RPG2_TOOL, now=10.0 + 2 * interval)
    assert "launcher_reload_skip" not in anticheat.counters(player)
    # The grace never compounds into a faster cadence.
    player = _launcher_player()
    assert Player.consume_oriented_item(player, C.RPG2_TOOL, now=20.0)
    assert Player.consume_oriented_item(player, C.RPG2_TOOL, now=20.0 + interval - 0.04)
    assert not Player.can_use_oriented_item(
        player, C.RPG2_TOOL, now=20.0 + 2 * interval - 0.08, report_violation=False
    )


def test_held_deployable_placement_admits_jitter_without_raising_the_rate():
    from server.deployable_inventory import (
        commit_deployable_use,
        deployable_ready,
        reset_deployable_inventory,
    )

    player = SimpleNamespace()
    reset_deployable_inventory(player)
    tool = int(C.LANDMINE_TOOL)
    interval = float(C.LANDMINE_SHOOT_INTERVAL)
    assert deployable_ready(player, tool, 5.0)
    commit_deployable_use(player, tool, 5.0)
    early = 5.0 + interval - 0.03
    assert deployable_ready(player, tool, early)
    commit_deployable_use(player, tool, early)
    # Scheduled from the due time (6.0), not from the early arrival.
    assert not deployable_ready(player, tool, 5.0 + 2 * interval - 0.06)
    assert deployable_ready(player, tool, 5.0 + 2 * interval)


def test_ugc_rpg2_never_spends_and_ugc_drill_reloads_with_an_endless_reserve():
    player = _launcher_player()
    ugc_rpg2 = int(C.UGC_RPG2_TOOL)
    for shot in range(20):
        assert Player.consume_oriented_item(player, ugc_rpg2, now=100.0 + 0.5 * shot)
    ugc_drill = int(C.UGC_DRILLGUN_TOOL)
    cycle = 0.2 + 4.0
    for shot in range(8):  # more than the 1 + 3 a stock drill could fire
        assert Player.consume_oriented_item(player, ugc_drill, now=200.0 + cycle * shot)
    # UGCDrillgunWeapon still reloads between rounds (DrillgunWeapon timing).
    assert not Player.can_use_oriented_item(
        player, ugc_drill, now=200.0 + cycle * 7 + 1.0, report_violation=False
    )
