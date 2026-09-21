"""Commando parachute loadout, activation, replication, and native physics."""

from types import SimpleNamespace

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

    # The shared Z input opens the chute but is not the UGC Builder hover
    # state.  Passing it through would skip gravity instead of applying the
    # parachute's recovered 0.05 gravity multiplier.
    world = SimpleNamespace(
        set_walk=lambda *args: None,
        set_crouch=lambda *args: None,
    )
    player._apply_input_state_to_world(
        trigger_jump=False,
        world_object=world,
        collisions=[],
    )
    assert world.hover is False

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
        # Enter airborne away from the solid map boundary. Jump must not
        # override the contact result returned by stock boxclipmove.
        body.set_position(100.5, 100.5, 100.0)
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


def test_death_retires_canopy_before_next_replicated_row():
    player = make_player()
    player.parachute_active = True
    player._parachute_deploy_last_held = True

    player.die()

    assert not player.parachute_active
    assert not player._parachute_deploy_last_held
    assert player.pack_state_flags() & 0x01 == 0


def test_unequipped_player_cannot_deploy_or_keep_a_canopy():
    player = make_player()
    player.parachute_id = 0
    player.airborne = True
    player.parachute_active = True
    player.update_action_input(False, False, hover=True)
    player._update_parachute()
    assert not player.parachute_active


def test_long_fall_deployed_chute_has_retail_descent_and_no_landing_damage():
    """Original mover: 0.05 gravity, ordinary drag, fall origin reset each tick."""
    from tests.test_reversed_world_update import make_player as network_player

    player, _ = network_player()
    player.class_id = int(C.CLASS_SOLDIER)
    player.loadout = [int(C.MINIGUN_TOOL), int(C.RPG_TOOL), int(C.A370)]
    player.spawn(100.5, 100.5, 19.75)  # Forty blocks above standing contact.
    world = player._ensure_world_object()
    world.update(DT, ())
    player._sync_cached_vectors()
    player.update_action_input(False, False, hover=True)
    player._update_parachute()
    assert player.parachute_active
    player._apply_input_state_to_world(False, world, [])

    results = []
    for frame in range(2400):
        results.append(world.update(DT, ()))
        if frame == 599:
            # v_z tends to 0.05 in native units: 32 * 0.05 = 1.6 blocks/s.
            assert world.velocity.z == pytest.approx(0.05, abs=0.00001)
        if not world.airborne:
            break
    assert not world.airborne
    assert 1200 < frame < 1800  # Controlled ~25-second descent, not free fall.
    assert all(result <= 0 for result in results)


def test_landing_retires_canopy_in_same_authority_tick():
    import asyncio
    from tests.test_reversed_world_update import make_player as network_player

    player, _ = network_player()
    player.loadout = [int(C.MINIGUN_TOOL), int(C.RPG_TOOL), int(C.A370)]
    player.spawn(100.5, 100.5, 59.7)
    world = player._ensure_world_object()
    world.set_velocity(0.0, 0.0, 0.1)
    player.airborne = True
    player.parachute_active = True
    asyncio.run(player.update(DT))

    assert not player.airborne
    assert not player.parachute_active
    assert not world.parachute_active
    assert player.pack_state_flags() & 0x01 == 0


def test_held_deploy_after_landing_does_not_protect_the_next_fall():
    """Landing must retire real authority immunity, not just its visual bit."""
    import asyncio
    from tests.test_reversed_world_update import make_player as network_player

    async def run():
        player, _ = network_player()
        player.loadout = [int(C.MINIGUN_TOOL), int(C.RPG_TOOL), int(C.A370)]
        player.spawn(100.5, 100.5, 59.7)
        world = player._ensure_world_object()
        world.set_velocity(0.0, 0.0, 0.1)
        player.airborne = True
        player.update_action_input(False, False, hover=True)
        await player.update(DT)
        assert not player.airborne
        assert not player.parachute_active
        assert player._parachute_deploy_last_held

        # A second fall while the same key is held has no new deploy edge.
        # The authoritative native result must report damage, even though the
        # player still owns equipment 72 and keeps sending the deploy input.
        player.set_position(100.5, 100.5, 29.75)
        world.set_velocity(0.0, 0.0, 0.0)
        for _ in range(360):
            await player.update(DT)
            assert not player.parachute_active
            assert not world.parachute_active
            if player.last_fall_result > 0:
                break
        assert player.last_fall_result > 0
        assert player.pack_state_flags() & 0x01 == 0

    asyncio.run(run())
