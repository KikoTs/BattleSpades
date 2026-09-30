"""Lag compensation over a simulated link: what the shooter saw, registered.

``scripts/lag_compensation_link.py`` replays the stock client's picture of a
moving target (newest snapshot received, simulated forward) over a link with
delay, jitter and loss, and asks the real server code whether the shot
registers. Everything is virtual time and seeded: no sockets, no clock.

Conditions from the wave 8 brief: 50 / 100 / 200 ms, +-20 ms jitter per
datagram and direction, 0.5-2 % loss, targets that strafe, jump, fly a
jetpack and descend on a parachute.
"""

from __future__ import annotations

import logging
import math

import pytest

import scripts.lag_compensation_link as model
from server import lag_compensation as lc


SECONDS = 20.0
SEED = 11
JITTER_MS = 20.0
PINGS = (50.0, 100.0, 200.0)


@pytest.fixture(scope="module")
def world():
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield model.build_world()
    finally:
        logging.disable(previous)


def _measure(world, pattern, rtt, loss=0.01, **kwargs):
    kwargs.setdefault("seconds", SECONDS)
    kwargs.setdefault("seed", SEED)
    return model.measure(
        world, pattern, model.LinkProfile(rtt, JITTER_MS, loss), **kwargs
    )


# ---------------------------------------------------------------------------
# The model itself
# ---------------------------------------------------------------------------


def test_paths_are_continuous_lives():
    """No segment may teleport: a teleport starts a new rewind epoch."""
    for name, build in model.PATTERNS.items():
        path = build(12.0)
        previous = path.state(0.0)[0]
        for tick in range(1, int(12.0 * 60)):
            position = path.state(tick / 60.0)[0]
            assert math.dist(position, previous) < lc.TELEPORT_BLOCKS_PER_TICK, (
                name, tick,
            )
            previous = position


def test_round_trip_model_follows_enet_smoothing():
    estimate = model.EnetRoundTrip()
    assert estimate.mean == 500  # ENet's default before any sample
    for _ in range(80):
        estimate.sample(100.0)
    assert 99 <= estimate.mean <= 108
    assert estimate.retransmit_ms >= estimate.mean


def test_same_seed_gives_the_same_shots(world):
    first = _measure(world, "jump", 100.0).summary()
    second = _measure(world, "jump", 100.0).summary()
    assert first == second
    assert first["shots"] > 100


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", sorted(model.PATTERNS))
@pytest.mark.parametrize("rtt", PINGS)
def test_shooter_hits_what_it_saw(world, pattern, rtt):
    summary = _measure(world, pattern, rtt).summary()

    # Shots whose packet arrived at the first attempt.
    assert summary["hit_rate_first_try"] >= 0.95, summary
    # Including the ones a loss delayed by a retransmission time-out.
    assert summary["hit_rate"] >= 0.93, summary
    # The body the server tested is the body on the shooter's screen.
    assert summary["view_error_mean"] <= 0.30, summary


@pytest.mark.parametrize("pattern", ["strafe", "jump"])
@pytest.mark.parametrize("rtt", [100.0, 200.0])
def test_without_compensation_the_same_shots_miss(world, pattern, rtt):
    summary = _measure(world, pattern, rtt).summary()

    assert summary["hit_rate_uncompensated"] <= 0.30, summary
    assert summary["hit_rate"] - summary["hit_rate_uncompensated"] >= 0.60


@pytest.mark.parametrize("loss", [0.005, 0.02])
def test_loss_costs_at_most_the_retransmitted_shots(world, loss):
    result = _measure(world, "strafe", 100.0, loss=loss, seconds=40.0)
    summary = result.summary()

    assert summary["hit_rate_first_try"] >= 0.95, summary
    # A retransmitted shot is older than the rewind allowance: it may miss,
    # and that is the whole cost of the loss.
    missed = result.shots - result.hits
    first_try_missed = (
        (result.shots - result.retransmitted)
        - (result.hits - result.retransmitted_hits)
    )
    assert missed - first_try_missed <= result.retransmitted


@pytest.mark.parametrize("interval", [2, 4, 6])
def test_paced_snapshots_keep_registration(world, interval):
    """The reorder guard spaces snapshots up to six ticks apart."""
    summary = _measure(
        world, "strafe", 100.0, snapshot_interval=interval
    ).summary()

    assert summary["hit_rate_first_try"] >= 0.93, summary


# ---------------------------------------------------------------------------
# The rewind estimate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rtt", PINGS)
def test_rewind_follows_the_view_age(world, rtt):
    result = _measure(world, "strafe", rtt, loss=0.0)
    signed = [
        rewind - ideal
        for rewind, ideal in zip(result.rewinds_ms, result.ideal_ms)
    ]
    mean = sum(signed) / len(signed)
    absolute = sum(abs(value) for value in signed) / len(signed)

    # The round trip overstates the view age by about 12 ms (half a tick of
    # wait plus the client's socket poll). Kept on purpose: see the note in
    # lag_compensation.rewind_targets.
    assert 4.0 <= mean <= 18.0, mean
    assert absolute <= 22.0, absolute


# ---------------------------------------------------------------------------
# Abuse bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rtt", PINGS)
def test_rewind_never_exceeds_the_allowance(world, rtt):
    server = world[0]
    result = _measure(world, "jetpack", rtt, loss=0.02, seconds=40.0)
    cap = float(server.config.lag_compensation_max_ms)
    extra = float(server.config.lag_compensation_extra_ms)

    assert max(result.rewinds_ms) <= cap + 1e-6
    # Never more than the measured round trip plus the allowance; ENet's
    # estimate stays within the jitter of the true one.
    assert max(result.rewinds_ms) <= rtt + 2 * JITTER_MS + 10.0 + extra


@pytest.mark.parametrize("pattern", sorted(model.PATTERNS))
def test_shot_behind_cover_distance_is_bounded(world, pattern):
    """How far from its present position a target can still be hit."""
    server = world[0]
    result = _measure(world, pattern, 200.0, loss=0.02)
    cap_seconds = float(server.config.lag_compensation_max_ms) / 1000.0
    path = model.PATTERNS[pattern](SECONDS + 6.0)
    fastest = max(
        math.hypot(*path.state(tick / 60.0)[1])
        for tick in range(int((SECONDS + 5.0) * 60))
    )

    assert max(result.cover_blocks) <= fastest * cap_seconds + 0.05


def test_forged_old_snapshot_gains_no_rewind(world):
    honest = _measure(world, "strafe", 100.0)
    forged = _measure(world, "strafe", 100.0, forged_snapshot_age=60)

    # Claiming a snapshot one second old: the round trip still bounds it.
    assert max(forged.rewinds_ms) <= max(honest.rewinds_ms) + 40.0
    assert max(forged.rewinds_ms) <= 100.0 + 2 * JITTER_MS + 10.0
    # An honest aim still registers under the forged claim's bound.
    assert forged.hits >= honest.hits - 3


def test_forged_fresh_snapshot_only_shortens_the_rewind(world):
    honest = _measure(world, "strafe", 200.0)
    forged = _measure(world, "strafe", 200.0, forged_snapshot_age=1)

    assert max(forged.rewinds_ms) <= (1000.0 / 60.0) + 1e-6
    assert forged.hits < honest.hits
