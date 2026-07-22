"""Commando parachute loadout, activation, replication, and native physics."""

import pytest

from aoslib.world import Player as WorldPlayer
from server.class_data import get_loadout
from server.player import Player
from shared import constants as C


DT = 1.0 / 60.0


def make_player() -> Player:
    player = Player(id=1, name="Test", team=3, weapon=int(C.RIFLE_TOOL), connection=None)
    player.class_id = int(C.CLASS_SOLDIER)
    player.loadout = [int(C.MINIGUN_TOOL), int(C.RPG_TOOL), int(C.A370)]
    player.spawn(10.0, 10.0, 10.0)
    return player


def test_commando_loadout_offers_normal_parachute():
    assert int(C.A370) in get_loadout(int(C.CLASS_SOLDIER)).equipment


def test_spawn_honors_commando_parachute_choice():
    player = make_player()

    assert player.parachute_id == int(C.A370)
    assert player.parachute_active is False


def test_active_parachute_is_replicated_in_world_update_state():
    player = make_player()
    player.parachute_active = True

    assert player.pack_state_flags() & 0x01


def test_parachute_deploys_from_airborne_z_hover_press():
    player = make_player()
    # SPACE remains ordinary jump and must not deploy the parachute.
    player.airborne = False
    player.update_input(False, False, False, False, True, False, False, False)
    player._update_parachute()
    assert player.parachute_active is False

    # The retail client's default Z binding arrives as the hover action bit.
    player.airborne = True
    player.update_action_input(False, False, hover=False)
    player._update_parachute()
    assert player.parachute_active is False
    player.update_action_input(False, False, hover=True)
    player._update_parachute()
    assert player.parachute_active is True

    # Holding Z keeps it open without retriggering; landing closes it.
    player._update_parachute()
    assert player.parachute_active is True
    player.airborne = False
    player._update_parachute()
    assert player.parachute_active is False


def test_falling_without_z_press_does_not_auto_deploy():
    player = make_player()
    player.airborne = True
    player.vz = 5.0
    player.jump_held = False
    player.jump_last_held = False
    player._update_parachute()
    assert player.parachute_active is False


def test_z_must_be_pressed_after_becoming_airborne():
    player = make_player()
    player.airborne = False
    player.update_action_input(False, False, hover=True)
    player._update_parachute()

    player.airborne = True
    player._update_parachute()
    assert player.parachute_active is False

    player.update_action_input(False, False, hover=False)
    player._update_parachute()
    player.update_action_input(False, False, hover=True)
    player._update_parachute()
    assert player.parachute_active is True


def test_world_parachute_matches_stock_gravity():
    normal = WorldPlayer(None)
    chute = WorldPlayer(None)
    for body in (normal, chute):
        # One no-map frame enters the native airborne state; the property is
        # intentionally read-only, matching the stock extension.
        body.jump = True
        body.update(DT, [])
        assert body.airborne is True
        body.set_velocity(1.0, 0.0, 0.0)
    chute.parachute = int(C.A370)
    chute.parachute_active = True

    normal.update(DT, [])
    chute.update(DT, [])

    # world.pyd Player.update @ 0x10012EFB: an active type-1 parachute
    # receives 0.05 * dt * gravity.  The separate 0.75 branch belongs to
    # passive jetpack/hover state, not the parachute.
    expected_chute_vz = (DT * 1.0 * 0.05) / (1.0 + DT)
    assert chute.velocity.z == pytest.approx(expected_chute_vz, abs=1e-6)
    assert chute.velocity.z < normal.velocity.z
