"""Replay mining phase independently from host uptime without stale cooldowns."""

import math

import pytest

from scripts.bot_map_matrix import simulation_clock_base


@pytest.mark.parametrize("setup", [0.0, 7.5, 100.01, 1_000_000.75])
def test_default_clock_phase_is_stable_after_all_actor_initialization(setup):
    base = simulation_clock_base(setup)
    assert base >= setup + 1
    assert base % 8 == 0
    assert base < setup + 9


def test_historical_failure_phase_replays_without_clock_reversal():
    previous = 292988.734
    base = simulation_clock_base(500000.125, replay_base=previous)
    assert base > 500000.125
    assert (base - previous) / 8 == round((base - previous) / 8)
    for tick in range(7200):
        # This is the actual phase-dependent mining choice from the original
        # failed London DIA simulation, for every one of its 120s /60Hz ticks.
        assert int((base + tick / 60) * 2) & 7 == int((previous + tick / 60) * 2) & 7


def test_future_override_keeps_the_exact_requested_epoch():
    assert simulation_clock_base(10, replay_base=40.25) == 40.25


@pytest.mark.parametrize("phase", [-1, 8, math.nan, math.inf])
def test_invalid_clock_phases_fail_closed(phase):
    with pytest.raises(ValueError):
        simulation_clock_base(10, phase=phase)


def test_nonfinite_replay_base_is_rejected():
    with pytest.raises(ValueError):
        simulation_clock_base(10, replay_base=math.nan)
