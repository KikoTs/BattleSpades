"""Regression cases found in the independent action-clock integration review."""

import shared.constants as C
import pytest
from server.combat_runtime import get_combat_system
from server.game_constants import TEAM1
from tests.test_reversed_combat import DummyServer, make_player, make_shoot_packet


def test_assault_burst_continuation_survives_a_reliable_packet_stall() -> None:
    server = DummyServer()
    player, _ = make_player(
        server, 0, "Delayed burst", TEAM1, C.ASSAULT_RIFLE_TOOL,
        (100.5, 100.5, 60.0),
    )
    player._input_newest_label = 600
    combat = get_combat_system(server)
    starting_ammo = player.ammo_clip

    # The weapon emitted a legal three-round burst over 0.2 client seconds.
    # A reliable-packet retransmission releases rounds 2 and 3 together.
    for label, arrival in ((100, 10.0), (106, 10.4), (112, 10.4)):
        packet = make_shoot_packet(player)
        packet.loop_count = label
        assert combat._accept_assault_burst_packet(player, packet, arrival)

    assert player.ammo_clip == starting_ammo - 3


@pytest.mark.parametrize("labels", [(100, 106, 112, 118), (100, 106, 604, 605)])
def test_a_stalled_burst_never_authorizes_a_forged_fourth_or_future_label(labels) -> None:
    server = DummyServer()
    player, _ = make_player(
        server, 0, "Bounded burst", TEAM1, C.ASSAULT_RIFLE_TOOL,
        (100.5, 100.5, 60.0),
    )
    player._input_newest_label = 600
    combat = get_combat_system(server)
    starting_ammo = player.ammo_clip
    results = []
    for index, label in enumerate(labels):
        packet = make_shoot_packet(player)
        packet.loop_count = label
        results.append(combat._accept_assault_burst_packet(
            player, packet, 10.0 if index == 0 else 10.4,
        ))
    expected = [True, True, True, False] if labels[-1] == 118 else [True, True, False, False]
    assert results == expected
    assert player.ammo_clip == starting_ammo - sum(expected)


def test_enforced_builds_share_one_budget_across_valid_and_invalid_labels(monkeypatch) -> None:
    from tests.test_building_reports import _builder

    server, player, _connection, _repaired = _builder(blocks=1000)
    server.config.anticheat.enforce_block_interval = True
    player._input_newest_label = 6000
    combat = get_combat_system(server)
    accepted = 0
    for step in range(100):
        for offset, label in ((0.0, 100 + step * 6), (0.081, -1)):
            arrival = 20.0 + step * 0.1 + offset
            monkeypatch.setattr("server.combat_runtime.time.monotonic", lambda: arrival)
            accepted += combat._block_interval_ok(player, ((101, 100, 60),), loop=label)

    # Ten seconds at the 0.1s minimum, plus the bounded default stall bucket.
    assert accepted <= 105
