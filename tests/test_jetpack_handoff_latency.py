"""Where the retail owner applies a jetpack transition row.

The stock client never acknowledges the WorldUpdate row that turns its pack
on or off; it simply thrusts from the frame on which the row was processed.
Eight live 60 Hz captures (loopback, 16 boundaries) measured that frame
against S, the label the server was simulating when it queued the row, and N,
the newest ClientData label it already held::

    thrust changed on N + 2   10 times
    thrust changed on N + 3    6 times     never earlier, never later

The fixed constants (ignition S + 3, exhaustion S + 4) equal those bounds
only while exactly one label is buffered and the link adds no delay.
``Player._jetpack_handoff_frames`` applies the bounds themselves.

The owner model below is that measured contract, not the client binary; a
live retail run at real ping is still owed.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from server.config import ServerConfig, load_config
from server.player import (
    JETPACK_ACTIVATION_DEFER_FRAMES,
    JETPACK_EXHAUSTION_TAIL_FRAMES,
    JETPACK_HANDOFF_LOCAL_RTT_MS,
    JETPACK_HANDOFF_MAX_FRAMES,
)
from tests.test_reversed_world_update import make_player


def _owner(*, applied=1000, newest=1001, rtt_ms=0.0, native=False, **config):
    player, connection = make_player()
    connection.peer = SimpleNamespace(roundTripTime=rtt_ms)
    connection.flight_profile_capable = native
    connection.server.config = SimpleNamespace(**config)
    player.last_applied_input_loop = applied
    player.input_history = {
        label: object() for label in range(applied + 1, newest + 1)
    }
    return player


def _ignition_label(player, applied):
    frames = player._jetpack_handoff_frames(
        "jetpack_activation_defer_frames",
        JETPACK_ACTIVATION_DEFER_FRAMES,
        latest=False,
    )
    return applied + 1 + frames  # first label simulated with thrust


def _stop_label(player, applied):
    frames = player._jetpack_handoff_frames(
        "jetpack_exhaustion_tail_frames",
        JETPACK_EXHAUSTION_TAIL_FRAMES,
        latest=True,
    )
    return applied + 1 + frames  # first label simulated without thrust


def test_one_buffered_label_and_no_delay_keeps_the_calibrated_constants():
    player = _owner(applied=1000, newest=1001)

    assert _ignition_label(player, 1000) == 1003  # S + 3
    assert _stop_label(player, 1000) == 1004      # S + 4


def test_no_buffered_label_never_shortens_the_constants():
    player = _owner(applied=1000, newest=1000)

    assert _ignition_label(player, 1000) == 1003
    assert _stop_label(player, 1000) == 1004


# (capture, S, N, owner label) exactly as measured; ignition then exhaustion.
IGNITION_CAPTURES = [
    ("d2_t1", 1126, 1127, 1130),
    ("d2_t3_11899", 1053, 1054, 1057),
    ("d2_t3_29543", 1107, 1109, 1111),
    ("d3_t2", 1133, 1134, 1136),
    ("d3_t3", 1121, 1122, 1125),
    ("d3_t3_10378", 1167, 1168, 1171),
    ("d3_t3_11837", 1132, 1133, 1135),
    ("d4_t3", 1123, 1125, 1127),
]
EXHAUSTION_CAPTURES = [
    ("d2_t1", 1198, 1199, 1201),
    ("d2_t3_11899", 1125, 1126, 1129),
    ("d2_t3_29543", 1179, 1181, 1183),
    ("d3_t2", 1205, 1206, 1208),
    ("d3_t3", 1193, 1194, 1197),
    ("d3_t3_10378", 1239, 1240, 1242),
    ("d3_t3_11837", 1204, 1205, 1207),
    ("d4_t3", 1195, 1196, 1198),
]


def test_recorded_ignitions_are_never_a_rollback_and_more_often_exact():
    """Server thrust must not start after the owner's (owner ahead)."""
    exact = fixed_exact = 0
    for _name, applied, newest, owner in IGNITION_CAPTURES:
        label = _ignition_label(_owner(applied=applied, newest=newest), applied)
        assert label <= owner
        assert owner - label <= 1
        exact += int(label == owner)
        fixed_exact += int(applied + 3 == owner)
    assert (fixed_exact, exact) == (2, 4)


def test_recorded_exhaustions_are_never_a_rollback():
    """Server thrust must not stop before the owner's (owner ahead)."""
    for _name, applied, newest, owner in EXHAUSTION_CAPTURES:
        label = _stop_label(_owner(applied=applied, newest=newest), applied)
        assert label >= owner
        assert label - owner <= 1 + max(0, newest - applied - 1)


@pytest.mark.parametrize("rtt_ms", [50.0, 100.0, 200.0])
@pytest.mark.parametrize("buffered", [1, 2, 4])
def test_round_trip_moves_both_boundaries_with_the_owner(rtt_ms, buffered):
    """The owner's clock runs on for one round trip before the row lands."""
    applied = 5000
    newest = applied + buffered
    player = _owner(applied=applied, newest=newest, rtt_ms=rtt_ms)
    network = (rtt_ms - JETPACK_HANDOFF_LOCAL_RTT_MS) * 60.0 / 1000.0
    # Measured contract shifted by the network delay, in whole owner frames.
    owner_earliest = newest + 2 + math.floor(network)
    owner_latest = newest + 3 + math.ceil(network)

    ignition = _ignition_label(player, applied)
    stop = _stop_label(player, applied)

    assert ignition == max(applied + 3, owner_earliest)
    assert stop == max(applied + 4, owner_latest)
    # Whatever frame the owner lands on, the error is bounded and one-sided.
    for owner in range(owner_earliest, owner_latest + 1):
        assert 0 <= owner - ignition <= owner_latest - owner_earliest
        assert 0 <= stop - owner <= owner_latest - owner_earliest
    # The fixed constants miss by the whole round trip.
    assert owner_earliest - (applied + 3) >= math.floor(network)


def test_native_owner_keeps_the_constants_it_predicts_with():
    player = _owner(applied=1000, newest=1006, rtt_ms=180.0, native=True)

    assert _ignition_label(player, 1000) == 1003
    assert _stop_label(player, 1000) == 1004


def test_switch_restores_the_fixed_constants():
    player = _owner(
        applied=1000, newest=1006, rtt_ms=180.0,
        jetpack_handoff_latency_aware=False,
    )

    assert _ignition_label(player, 1000) == 1003
    assert _stop_label(player, 1000) == 1004


def test_host_override_is_the_floor_the_bounds_extend():
    player = _owner(
        applied=1000, newest=1001, jetpack_activation_defer_frames=5,
        jetpack_exhaustion_tail_frames=1,
    )

    assert _ignition_label(player, 1000) == 1006
    assert _stop_label(player, 1000) == 1004  # N + 3 despite tail 1


def test_handoff_is_bounded_on_a_broken_round_trip():
    for rtt in (5000.0, float("nan"), -40.0, float("inf")):
        player = _owner(applied=1000, newest=1001, rtt_ms=rtt)
        ignition = _ignition_label(player, 1000)
        stop = _stop_label(player, 1000)
        assert 1003 <= ignition <= 1001 + JETPACK_HANDOFF_MAX_FRAMES
        assert 1004 <= stop <= 1001 + JETPACK_HANDOFF_MAX_FRAMES


def test_config_key_is_loaded(tmp_path):
    assert ServerConfig().jetpack_handoff_latency_aware is True
    path = tmp_path / "c.toml"
    path.write_text(
        "[debug]\njetpack_handoff_latency_aware = false\n", encoding="utf-8"
    )
    assert load_config(path).jetpack_handoff_latency_aware is False
