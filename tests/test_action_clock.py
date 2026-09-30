"""Network stalls preserve action cadence without allowing sustained rapid fire."""

from types import SimpleNamespace

import pytest

import shared.constants as C
from server import action_clock
from server.combat_runtime import get_combat_system
from server.game_constants import TEAM1
from server.player import Player
from tests.test_reversed_combat import DummyServer, make_player, make_shoot_packet


def clock_player() -> SimpleNamespace:
    return SimpleNamespace(_input_newest_label=600, connection=None)


def test_reliable_batch_preserves_the_clients_original_spacing() -> None:
    player = clock_player()
    # Three legal 10 Hz actions delivered together after a reliable stall.
    for label in (100, 106, 112):
        assert action_clock.admit(player, "fire", label=label, interval=0.1, now=20.0)
    # The second mouse button in that same frame cannot add another action.
    assert not action_clock.admit(player, "fire", label=112, interval=0.1, now=20.0)


@pytest.mark.parametrize("label", [None, -1, 604, float("inf"), "bad"])
def test_implausible_labels_cannot_bypass_arrival_cooldown(label: object) -> None:
    player = clock_player()
    assert action_clock.admit(player, "fire", label=100, interval=0.4, now=20.0)
    assert not action_clock.admit(player, "fire", label=label, interval=0.4, now=20.005)


def test_forged_clock_cannot_sustain_more_than_the_server_rate() -> None:
    player = clock_player()
    accepted = 0
    for step in range(6001):
        player._input_newest_label = step * 100
        accepted += action_clock.admit(
            player, "fire", label=step * 100, interval=0.4, now=100.0 + step / 1000.0
        )
    # Six seconds at 2.5 Hz, plus the bounded two-action stall allowance.
    assert accepted <= 17


def test_peek_does_not_consume_or_create_an_action_lane() -> None:
    player = clock_player()
    for _ in range(100):
        assert action_clock.peek(player, "fire", label=100, interval=0.1, now=20.0)
    assert not action_clock.lanes(player)
    assert action_clock.admit(player, "fire", label=100, interval=0.1, now=20.0)


def test_switching_to_a_faster_tool_cannot_shorten_the_previous_swing() -> None:
    player = clock_player()
    assert action_clock.admit(player, "fire", label=100, interval=0.4, now=20.0)
    assert not action_clock.admit(player, "fire", label=106, interval=0.1, now=20.1)
    assert action_clock.admit(player, "fire", label=124, interval=0.1, now=20.4)


def test_alternating_weapon_rates_cannot_manufacture_cooldown_credit() -> None:
    player = clock_player()
    admitted_seconds = 0.0
    for step in range(6001):
        player._input_newest_label = step * 100
        interval = 0.4 if step % 2 == 0 else 0.1
        if action_clock.admit(
            player, "fire", label=step * 100,
            interval=interval, now=100.0 + step / 1000.0,
        ):
            admitted_seconds += interval
    assert admitted_seconds <= 6.8 + 1e-8


def test_fire_packet_path_uses_labels_and_shares_primary_secondary(monkeypatch) -> None:
    server = DummyServer()
    player, _ = make_player(server, 0, "Timing", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player._input_newest_label = 600
    monkeypatch.setattr("server.combat_runtime.time.monotonic", lambda: 200.0)
    interval = player.get_weapon_profile().fire_interval
    frames = action_clock.interval_frames(interval)
    before = player.ammo_clip
    combat = get_combat_system(server)
    for label in (100, 100 + frames):
        packet = make_shoot_packet(player)
        packet.loop_count = label
        # handle_shot returns whether a ray hit anything; ammunition proves
        # admission even in this empty range.
        combat.handle_shot(player, packet)
    assert player.ammo_clip == before - 2
    packet.secondary = 1
    assert not combat.handle_shot(player, packet)
    assert player.ammo_clip == before - 2


def test_new_life_clears_action_debt() -> None:
    player = Player(1, "Timing", TEAM1, C.RIFLE_TOOL, None)
    player.spawn(100.5, 100.5, 60.0)
    player._input_newest_label = 600
    assert player.consume_shot(now=100.0, loop=500)
    assert not player.consume_shot(now=100.001, loop=500)
    player.spawn(100.5, 100.5, 60.0)
    assert player.consume_shot(now=100.001, loop=500)


def test_shot_labels_do_not_finish_a_magazine_reload_early() -> None:
    player = Player(1, "Timing", TEAM1, C.RIFLE_TOOL, None)
    player.spawn(100.5, 100.5, 60.0)
    player.ammo_clip = 0
    player._input_newest_label = 600
    assert player.start_reload(now=100.0)
    # WeaponReload has no loop label: shot timestamps cannot backdate it.
    assert not player.consume_shot(now=100.01, loop=600)
    assert player.ammo_clip == 0 and player.reloading


def test_enforced_build_interval_accepts_a_stalled_batch_but_repairs_duplicate(monkeypatch) -> None:
    from tests.test_building_reports import _builder, _line

    server, player, _connection, repaired = _builder(blocks=10)
    server.config.anticheat.enforce_block_interval = True
    player._input_newest_label = 600
    monkeypatch.setattr("server.combat_runtime.time.monotonic", lambda: 200.0)
    combat = get_combat_system(server)
    for label, y in ((100, 99), (106, 100), (106, 101)):
        server.world_manager.set_block(101, y, 61, True, (80, 80, 80))
        cell = (101, y, 60)
        packet = _line(player, cell, cell)
        packet.loop_count = label
        accepted = combat.handle_block_line(player, packet)
        assert accepted is (y != 101)
    assert player.blocks == 8
    assert repaired == [(101, 101, 60)]
