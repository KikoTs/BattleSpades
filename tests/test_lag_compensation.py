"""Lag compensation: rewound target hitboxes for shots under ping.

A shooter with RTT R saw remote bodies ~R old (the retail client
extrapolates remotes from the newest WorldUpdate; the shot then travels
upstream). These tests drive the real Player/CombatRuntime hitbox code with
``RewindContext.body`` substituted for the live target, which is exactly the
hook combat_runtime installs in ``_find_first_player_hit``.
"""

import inspect
import math
import os
import time
from types import SimpleNamespace

import pytest

import shared.constants as C
from server import lag_compensation as lc
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.player import Player
from shared.packet import ShootPacket


EYE_Z = 59.75
SPEED = 0.3  # blocks per tick along +x (18 blocks/s: a fast strafe)


class _Connection:
    def __init__(self, server):
        self.server = server
        self.player = None
        self.in_game = True
        self.sent = []
        self.reserved_player_id = None
        self.peer = None

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append((data, reliable))

    def on_disconnect(self):
        pass


def _server():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    return server


def _player(server, *, player_id, team, position, tool=C.RIFLE_TOOL,
            rtt_ms=None, bot=False):
    connection = _Connection(server)
    player = Player(player_id, f"LC{player_id}", team, C.RIFLE_TOOL, connection)
    connection.player = player
    if rtt_ms is not None:
        connection.peer = SimpleNamespace(roundTripTime=rtt_ms)
    player.is_bot = bot
    player.class_id = int(C.CLASS_SOLDIER)
    player.loadout = [tool]
    player.spawn(*position)
    player.set_tool(tool, raw=True)
    player.spawned_at = time.monotonic() - 60.0
    server.players[player.id] = player
    server.connections[player.id] = connection
    server.teams[team].add_player(player)
    return player


def _target_x(label):
    return 95.5 + SPEED * label


def _run_target(server, target, ticks):
    """Simulate ``ticks`` server ticks of a target strafing along +x.

    Mirrors SimulationRuntime: loop_count += 1, then (packet drain), then
    Player.simulate_tick records label loop_count-1 before moving the body.
    After the loop the live body is label ``ticks`` and the next tick's
    packet drain (loop_count = ticks + 1) is where the shot is handled.
    """

    target.set_position(_target_x(0), 115.5, EYE_Z)
    for tick in range(1, ticks + 1):
        server.loop_count = tick
        lc.record_player(target)
        target.set_position(_target_x(tick), 115.5, EYE_Z)
    server.loop_count = ticks + 1


def _aim_at(origin, point, height=0.8):
    delta = (point[0] - origin[0], point[1] - origin[1],
             point[2] + height - origin[2])
    length = math.sqrt(sum(c * c for c in delta))
    return tuple(c / length for c in delta)


def _packet(shooter, direction, *, snapshot=0, loop=1):
    packet = ShootPacket()
    packet.loop_count = loop
    packet.shooter_id = shooter.id
    packet.shot_on_world_update = snapshot
    packet.x, packet.y, packet.z = shooter.eye
    packet.ori_x, packet.ori_y, packet.ori_z = direction
    packet.damage = 0.0
    packet.penetration = 0
    packet.affect_shooter = 0
    packet.secondary = 0
    packet.seed = 0
    return packet


def _hits(server, shooter, direction, body):
    return server.combat._ray_hits_target(
        shooter.eye, direction, 128.0, body) is not None


def _setup(rtt_ms=100.0, ticks=30, bot_shooter=False, bot_target=False):
    server = _server()
    shooter = _player(server, player_id=0, team=TEAM1,
                      position=(100.5, 100.5, EYE_Z), rtt_ms=rtt_ms,
                      bot=bot_shooter)
    target = _player(server, player_id=1, team=TEAM2,
                     position=(_target_x(0), 115.5, EYE_Z), bot=bot_target)
    _run_target(server, target, ticks)
    return server, shooter, target


def _seen(label):
    return (_target_x(label), 115.5, EYE_Z)


# --- core behaviour ---------------------------------------------------------

def test_moving_target_hit_with_compensation_and_missed_without():
    server, shooter, target = _setup(rtt_ms=100.0, ticks=30)
    now = lc.current_tick(server)
    assert now == 30
    # 100 ms at 60 Hz = 6 ticks: the shooter saw label 24.
    direction = _aim_at(shooter.eye, _seen(now - 6))
    packet = _packet(shooter, direction, snapshot=now - 7)

    assert not _hits(server, shooter, direction, target)

    ctx = lc.rewind_targets(server, shooter, packet)
    assert ctx is not None
    assert ctx.rewind_ms == pytest.approx(100.0)
    body = ctx.body(target)
    assert isinstance(body, lc.RewoundBody)
    assert body.x == pytest.approx(_target_x(now - 6))
    assert _hits(server, shooter, direction, body)
    # The live body is untouched (nothing to restore).
    assert target.x == pytest.approx(_target_x(now))
    assert body.class_id == target.class_id and body.id == target.id


def test_snapshot_floor_corrects_overestimated_rtt():
    # ENet starts peers at a 500 ms default; the claimed snapshot bounds the
    # view (the client extrapolates FORWARD from it, never shows older).
    server, shooter, target = _setup(rtt_ms=500.0, ticks=40)
    now = lc.current_tick(server)
    direction = _aim_at(shooter.eye, _seen(now - 4))
    ctx = lc.rewind_targets(server, shooter,
                            _packet(shooter, direction, snapshot=now - 4))
    assert ctx.rewind_ms == pytest.approx(4 * 1000.0 / 60.0)
    assert _hits(server, shooter, direction, ctx.body(target))


def test_fractional_rewind_interpolates_between_ticks():
    server, shooter, target = _setup(rtt_ms=75.0, ticks=30)
    now = lc.current_tick(server)
    ctx = lc.rewind_targets(server, shooter, _packet(shooter, (0, 1, 0)))
    ticks = 75.0 / (1000.0 / 60.0)  # 4.5
    assert ctx.body(target).x == pytest.approx(_target_x(now - ticks))


# --- clamps / anti-abuse ----------------------------------------------------

def test_clamp_prevents_one_second_backtrack():
    server, shooter, target = _setup(rtt_ms=100.0, ticks=63)
    now = lc.current_tick(server)
    one_second_ago = now - 60
    direction = _aim_at(shooter.eye, _seen(one_second_ago + 1))
    packet = _packet(shooter, direction, snapshot=one_second_ago)
    ctx = lc.rewind_targets(server, shooter, packet)
    # RTT bounds the rewind; the claimed ancient snapshot grants nothing.
    assert ctx.rewind_ms <= 100.0 + 1e-6
    assert not _hits(server, shooter, direction, ctx.body(target))
    counts = getattr(shooter, "anticheat_counts", {})
    assert counts.get("lag_comp_stale_snapshot:observed", 0) == 1


def test_absolute_cap_limits_high_ping_rewind():
    server, shooter, target = _setup(rtt_ms=1000.0, ticks=63)
    now = lc.current_tick(server)
    ctx = lc.rewind_targets(server, shooter, _packet(shooter, (0, 1, 0)))
    assert ctx.rewind_ms == pytest.approx(250.0)
    assert ctx.allowed_ms == pytest.approx(250.0)
    assert ctx.body(target).x == pytest.approx(_target_x(now - 15))


def test_config_keys_respected():
    server, shooter, target = _setup(rtt_ms=200.0, ticks=30)
    server.config.lag_compensation_max_ms = 50.0
    ctx = lc.rewind_targets(server, shooter, _packet(shooter, (0, 1, 0)))
    assert ctx.rewind_ms == pytest.approx(50.0)
    server.config.lag_compensation_enabled = False
    assert lc.rewind_targets(server, shooter, _packet(shooter, (0, 1, 0))) is None


def test_future_snapshot_is_ignored_and_reported():
    server, shooter, target = _setup(rtt_ms=100.0, ticks=30)
    ctx = lc.rewind_targets(server, shooter,
                            _packet(shooter, (0, 1, 0), snapshot=10_000))
    assert ctx.rewind_ms == pytest.approx(100.0)
    counts = getattr(shooter, "anticheat_counts", {})
    assert counts.get("lag_comp_future_snapshot:observed", 0) == 1


# --- death / respawn / teleport --------------------------------------------

def test_respawn_between_frames_is_not_rewound():
    server, shooter, target = _setup(rtt_ms=100.0, ticks=30)
    now = lc.current_tick(server)
    direction = _aim_at(shooter.eye, _seen(now - 6))
    target.die()
    target.spawn(140.5, 140.5, EYE_Z)
    ctx = lc.rewind_targets(server, shooter, _packet(shooter, direction))
    assert ctx.body(target) is target
    assert not _hits(server, shooter, direction, ctx.body(target))


def test_dead_sample_is_not_used():
    server = _server()
    shooter = _player(server, player_id=0, team=TEAM1,
                      position=(100.5, 100.5, EYE_Z), rtt_ms=100.0)
    target = _player(server, player_id=1, team=TEAM2,
                     position=(_target_x(0), 115.5, EYE_Z))
    for tick in range(1, 31):
        server.loop_count = tick
        # Dead (same life) at the view tick: e.g. corpse still replicated.
        target.alive = not (20 <= tick <= 26)
        lc.record_player(target)
        target.set_position(_target_x(tick), 115.5, EYE_Z)
    target.alive = True
    server.loop_count = 31
    ctx = lc.rewind_targets(server, shooter, _packet(shooter, (0, 1, 0)))
    assert ctx.body(target) is target


def test_teleport_is_not_rewound_across():
    server, shooter, target = _setup(rtt_ms=100.0, ticks=30)
    now = lc.current_tick(server)
    direction = _aim_at(shooter.eye, _seen(now - 6))
    # Teleport after the newest sample.
    target.set_position(20.5, 20.5, EYE_Z)
    ctx = lc.rewind_targets(server, shooter, _packet(shooter, direction))
    assert ctx.body(target) is target

    # Teleport inside the history window: the new epoch hides older samples.
    server, shooter, target = _setup(rtt_ms=100.0, ticks=30)
    target.set_position(20.5, 20.5, EYE_Z)
    for tick in range(31, 34):
        server.loop_count = tick
        lc.record_player(target)
    server.loop_count = 34
    ctx = lc.rewind_targets(server, shooter, _packet(shooter, direction))
    body = ctx.body(target)
    assert body is target or body.x == pytest.approx(20.5)


# --- bots ------------------------------------------------------------------

def test_bot_shooter_gets_no_rewind_but_bot_targets_are_recorded():
    server, shooter, target = _setup(rtt_ms=None, ticks=30, bot_shooter=True)
    assert lc.rewind_targets(server, shooter, _packet(shooter, (0, 1, 0))) is None

    server, shooter, target = _setup(rtt_ms=100.0, ticks=30, bot_target=True)
    now = lc.current_tick(server)
    ctx = lc.rewind_targets(server, shooter, _packet(shooter, (0, 1, 0)))
    assert ctx.body(target).x == pytest.approx(_target_x(now - 6))


def test_simulate_tick_records_history_for_bots_and_humans():
    import asyncio

    server = _server()
    human = _player(server, player_id=0, team=TEAM1,
                    position=(100.5, 100.5, EYE_Z))
    bot = _player(server, player_id=1, team=TEAM2,
                  position=(110.5, 110.5, EYE_Z), bot=True)
    server.loop_count = 5
    for player in (human, bot):
        asyncio.run(player.simulate_tick(server.tick_interval))
        sample = player._lag_history.get(4)
        assert sample is not None and sample[lc._LIVE]


def test_zero_rtt_human_gets_no_rewind():
    server, shooter, target = _setup(rtt_ms=0.0, ticks=30)
    assert lc.rewind_targets(server, shooter, _packet(shooter, (0, 1, 0))) is None


def test_compensated_context_installs_and_clears():
    server, shooter, target = _setup(rtt_ms=100.0, ticks=30)
    owner = SimpleNamespace()
    with lc.compensated(owner, server, shooter, _packet(shooter, (0, 1, 0))) as ctx:
        assert owner._lag_rewind is ctx
        assert lc.body_for(owner, target) is ctx.body(target)
    assert owner._lag_rewind is None
    assert lc.body_for(owner, target) is target


# --- end to end (active once combat_runtime carries the hook) --------------

def _hooked():
    from server.combat_runtime import CombatSystem

    try:
        source = inspect.getsource(CombatSystem._find_first_player_hit)
    except (OSError, TypeError, AttributeError):
        return False
    return "_lag_rewind" in source or "lag_compensation" in source


@pytest.mark.skipif(not _hooked(), reason="combat_runtime hook not installed yet")
def test_handle_shot_end_to_end_under_ping():
    server, shooter, target = _setup(rtt_ms=100.0, ticks=30)
    now = lc.current_tick(server)
    direction = _aim_at(shooter.eye, _seen(now - 6))
    shooter.orientation = direction
    shooter.next_shot_time = 0.0
    shooter.last_shot_time = 0.0
    health = target.health
    server.combat.handle_shot(shooter, _packet(shooter, direction,
                                                snapshot=now - 7))
    assert target.health < health
    assert target.x == pytest.approx(_target_x(now))

    # Same shot without compensation misses.
    server.config.lag_compensation_enabled = False
    target.health = health
    shooter.next_shot_time = 0.0
    shooter.last_shot_time = 0.0
    server.combat.handle_shot(shooter, _packet(shooter, direction,
                                                snapshot=now - 7))
    assert target.health == health


# --- overhead ---------------------------------------------------------------

# Shared CI runners (notably the Intel macOS image) run this micro-benchmark
# several times slower than a desktop; 0.072 ms was measured there against the
# 0.05 ms desktop budget. Keep the desktop budget locally and give CI headroom.
_CI_SLACK = 4.0 if os.environ.get("CI") else 1.0


def test_overhead_recording_and_rewind_are_cheap():
    server = _server()
    players = [
        _player(server, player_id=i, team=TEAM1 if i % 2 else TEAM2,
                position=(60.5 + 3 * i, 100.5, EYE_Z),
                rtt_ms=100.0)
        for i in range(24)
    ]
    ticks = 600
    best = float("inf")
    for _attempt in range(3):
        start = time.perf_counter()
        for tick in range(ticks):
            server.loop_count = tick + 1
            for player in players:
                lc.record_player(player)
        best = min(best, (time.perf_counter() - start) / ticks)
    assert best * 1000.0 < 0.05 * _CI_SLACK, f"record per tick {best * 1e3:.4f} ms"

    shooter = players[0]
    packet = _packet(shooter, (0, 1, 0))
    best = float("inf")
    for _attempt in range(3):
        start = time.perf_counter()
        for _ in range(200):
            ctx = lc.rewind_targets(server, shooter, packet)
            for player in players[1:]:
                ctx.body(player)
        best = min(best, (time.perf_counter() - start) / 200)
    assert best * 1000.0 < 0.25 * _CI_SLACK, f"rewind per shot {best * 1e3:.4f} ms"
